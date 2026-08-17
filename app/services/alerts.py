"""Global squawk-7700 watch.

Unlike the overhead poller (a 60 NM bubble around home), this polls an
aggregator's squawk endpoint for every aircraft in the world currently
squawking 7700, so emergencies show up regardless of where they are.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

import httpx

from app import config
from app.services.poller import cardinal, haversine_nm

log = logging.getLogger(__name__)

# adsb.lol is the only source here. airplanes.live's squawk endpoint went 403
# with the rest of their public API (16 Aug 2026) and adsb.fi v3 has no global
# squawk filter (/api/v3/sqk/ returns 400), so the 7700 watch has no fallback:
# an adsb.lol outage means no global alerts until it returns.
SQUAWK_URLS = [
    "https://api.adsb.lol/v2/squawk/7700",
]
# Product mode takes the commercially-licensed sources only - identical to the
# list above today, but keeps the split if a non-commercial fallback is added.
SQUAWK_URLS_PRODUCT = SQUAWK_URLS[:1]
GEO_URL = ("https://api.bigdatacloud.net/data/reverse-geocode-client"
           "?latitude={lat}&longitude={lon}&localityLanguage=en")
POLL_SECONDS = 60
# Per-client cap. Held aircraft are kept unsorted-by-anyone's-home until
# snapshot_for() ranks them for the requester, so a device in Europe served by
# an Australian-configured server sees the alerts nearest *it*. The stored list
# is capped separately (MAX_TRACKED) purely to bound memory - worldwide 7700s
# are normally 0-3, so neither cap usually bites.
MAX_ALERTS = 10
MAX_TRACKED = 60
ROUTE_LOOKUPS_PER_POLL = 5
# Demo 7700 lifetime: long enough to watch the 2-min takeover demote and rotate
DEMO_ALERT_SECONDS = 300


class GlobalAlerts:
    def __init__(self, client: httpx.AsyncClient, meta=None, product: bool = False):
        self._client = client
        self._meta = meta  # reused for route lookups
        self._squawk_urls = SQUAWK_URLS_PRODUCT if product else SQUAWK_URLS
        self._route_cache: dict[str, dict | None] = {}
        self._place_cache: dict[tuple, str | None] = {}
        self._demo_started = 0.0
        self._demo_until = 0.0
        self.snapshot: dict = {"aircraft": [], "count": 0, "updated": None}

    def trigger_demo(self) -> None:
        """Demo mode: fabricate a far-away 7700 for a few minutes."""
        now = time.time()
        self._demo_started = now
        self._demo_until = now + DEMO_ALERT_SECONDS
        aircraft = ([self._demo_alert()]
                    + [a for a in self.snapshot["aircraft"] if a["hex"] != "dem700"])
        self.snapshot = {"aircraft": aircraft, "count": len(aircraft),
                         "updated": int(now)}

    def _demo_alert(self) -> dict:
        """Mid-Tasman NZ103 squawking 7700, drifting along its track."""
        track, gs = 231.0, 470.0
        d_nm = gs * (time.time() - self._demo_started) / 3600.0
        lat = -29.2 + d_nm * math.cos(math.radians(track)) / 60.0
        lon = 159.8 + d_nm * math.sin(math.radians(track)) / (60.0 * math.cos(math.radians(-29.2)))
        return {
            "hex": "dem700",
            "callsign": "ANZ103",
            "registration": "ZK-NZE",
            "type": "B789",
            "description": "BOEING 787-9",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "altitude_ft": 21000,
            "ground_speed_kt": gs,
            "track": track,
            "heading_cardinal": cardinal(track),
            "squawk": "7700",
            "emergency": "general",
            "distance_nm": round(haversine_nm(config.HOME_LAT, config.HOME_LON, lat, lon), 1),
            "route": {"origin": "AKL", "origin_name": "Auckland",
                      "destination": "SYD", "destination_name": "Sydney",
                      "airline": "Air New Zealand", "airline_iata": "NZ"},
            "place": "Tasman Sea",
        }

    def snapshot_for(self, lat: float, lon: float) -> dict:
        """The global snapshot with distances recomputed from the requester's
        location (device fleets: each unit has its own home)."""
        aircraft = []
        for a in self.snapshot["aircraft"]:
            a = dict(a)
            if a.get("lat") is not None and a.get("lon") is not None:
                a["distance_nm"] = round(haversine_nm(lat, lon, a["lat"], a["lon"]), 1)
            aircraft.append(a)
        aircraft.sort(key=lambda a: a.get("distance_nm") or 9e9)
        aircraft = aircraft[:MAX_ALERTS]
        return {**self.snapshot, "aircraft": aircraft, "count": len(aircraft)}

    async def run(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:
                log.exception("global alerts poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _poll_once(self) -> None:
        raw = None
        for url in self._squawk_urls:
            try:
                resp = await self._client.get(url, timeout=15)
                resp.raise_for_status()
                raw = resp.json().get("ac") or []
                break
            except Exception as exc:
                log.warning("alerts fetch %s failed: %s", url, exc)
        if raw is None:
            return  # keep the previous snapshot rather than flapping to empty

        # Airborne only. Ranked by distance from the server's own home purely to
        # decide which few get the (rate-limited) route/place enrichment and
        # which get dropped if the world is unusually busy; per-client ranking
        # and the display cap happen in snapshot_for().
        aircraft = [a for a in (self._normalize(ac) for ac in raw) if a]
        aircraft.sort(key=lambda a: a.get("distance_nm") or 9e9)
        aircraft = aircraft[:MAX_TRACKED]
        await self._enrich_routes(aircraft)
        await self._enrich_places(aircraft)
        if time.time() < self._demo_until:
            aircraft.insert(0, self._demo_alert())  # demo alert always shown first
        if aircraft or self.snapshot["aircraft"]:
            log.info("global 7700 watch: %d active", len(aircraft))
        self.snapshot = {"aircraft": aircraft, "count": len(aircraft),
                         "updated": int(time.time())}

    def _normalize(self, ac: dict) -> dict | None:
        lat, lon = ac.get("lat"), ac.get("lon")
        alt = ac.get("alt_baro")
        if alt == "ground":
            return None  # squawking 7700 on the apron is a test/mistake
        dist = None
        if lat is not None and lon is not None:
            dist = round(haversine_nm(config.HOME_LAT, config.HOME_LON, lat, lon), 1)
        track = ac.get("track", ac.get("true_heading"))
        callsign = (ac.get("flight") or "").strip() or None
        return {
            "hex": ac.get("hex"),
            "callsign": callsign,
            "registration": ac.get("r"),
            "type": ac.get("t"),
            "description": ac.get("desc"),
            "lat": lat,
            "lon": lon,
            "altitude_ft": alt if isinstance(alt, (int, float)) else None,
            "ground_speed_kt": ac.get("gs"),
            "track": track,
            "heading_cardinal": cardinal(track),
            "squawk": ac.get("squawk") or "7700",
            "emergency": ac.get("emergency"),
            "distance_nm": dist,
            "route": None,
            "place": None,
        }

    async def _enrich_routes(self, aircraft: list[dict]) -> None:
        if self._meta is None:
            return
        for a in aircraft[:ROUTE_LOOKUPS_PER_POLL]:
            cs = a.get("callsign")
            if not cs:
                continue
            if cs not in self._route_cache:
                try:
                    self._route_cache[cs] = await self._meta.fetch_route(cs)
                except Exception:
                    self._route_cache[cs] = None
            a["route"] = self._route_cache[cs]
        if len(self._route_cache) > 500:
            self._route_cache.clear()

    async def _enrich_places(self, aircraft: list[dict]) -> None:
        """Reverse-geocode 'city, country' so displays can say where it is."""
        for a in aircraft[:ROUTE_LOOKUPS_PER_POLL]:
            lat, lon = a.get("lat"), a.get("lon")
            if lat is None or lon is None:
                continue
            key = (round(lat), round(lon))  # ~50 NM cells - plenty for a label
            if key not in self._place_cache:
                place = None
                try:
                    resp = await self._client.get(GEO_URL.format(lat=lat, lon=lon), timeout=10)
                    resp.raise_for_status()
                    j = resp.json()
                    city = j.get("city") or j.get("principalSubdivision") or j.get("locality")
                    country = j.get("countryName") or ""
                    if len(country) > 20:  # formal names get unwieldy on displays
                        country = j.get("countryCode") or country
                    place = ", ".join(x for x in (city, country) if x) or None
                except Exception as exc:
                    log.warning("reverse geocode failed: %s", exc)
                self._place_cache[key] = place
                if len(self._place_cache) > 300:
                    self._place_cache.clear()
            a["place"] = self._place_cache[key]
