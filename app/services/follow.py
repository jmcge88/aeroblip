"""Follow a flight anywhere in the world - "when does Nana's plane land?".

Each followed callsign is polled globally via adsb.lol's per-callsign
endpoint on the same shared throttle budget as everything else that talks to
adsb.lol. Follows are polled round-robin - one upstream request per
FOLLOW_POLL_SECONDS regardless of how many are active - and positions are
dead-reckoned between polls, so the display still moves.

Route enrichment reuses the metadata provider; origin/destination airport
coordinates come from the standing-data airports table, which is what turns
"a dot on a map" into a great-circle progress bar and an ETA.

Follows persist to DATA_DIR/follows.json and expire after EXPIRE_S. Oceanic
flights drop off ADS-B coverage for hours; a follow that loses its aircraft
keeps the last fix and reports "no coverage" rather than pretending the
flight ceased to exist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path

import httpx

from app import config
from app.providers import radar
from app.services.poller import bearing_deg, cardinal, dead_reckon, haversine_nm

log = logging.getLogger(__name__)

CALLSIGN_URL = "https://api.adsb.lol/v2/callsign/{cs}"
BUDGET = "adsblol"  # shares app.providers.radar's global throttle/penalties
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,8}$")
EXPIRE_S = 24 * 3600
LOST_AFTER_S = 600       # live -> no_coverage after this long without a fix
LANDED_REMOVE_S = 1800   # landed follows clean themselves up
EXTRAP_MAX_S = 150.0     # round-robin polls are slow; reckon a bit further

# Demo follow: a fabricated flight two-thirds of the way Singapore -> Brisbane
DEMO_ORIGIN = ("SIN", "Singapore", 1.359, 103.989)
DEMO_DEST = ("BNE", "Brisbane", -27.384, 153.117)
DEMO_START_FRAC = 0.62
DEMO_GS = 480.0


def _gc_point(lat1: float, lon1: float, lat2: float, lon2: float,
              f: float) -> tuple[float, float]:
    """Point a fraction f along the great circle between two coordinates."""
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return (math.degrees(math.atan2(z, math.hypot(x, y))),
            math.degrees(math.atan2(y, x)))


class TooManyFollows(Exception):
    pass


class FollowTracker:
    def __init__(self, client: httpx.AsyncClient, meta, standing,
                 path: str | Path):
        self._client = client
        self._meta = meta
        self._standing = standing
        self._path = Path(path)
        self._follows: dict[str, dict] = {}
        self._rr: list[str] = []  # round-robin queue of callsigns to poll
        self.updated: int | None = None
        try:
            if self._path.exists():
                for f in json.loads(self._path.read_text())["follows"]:
                    if CALLSIGN_RE.fullmatch(f.get("callsign", "")):
                        self._follows[f["callsign"]] = self._new_state(
                            f["callsign"], f.get("added") or int(time.time()))
        except (OSError, ValueError, KeyError):
            log.exception("follows file unreadable - starting empty")

    @staticmethod
    def _new_state(cs: str, added: int) -> dict:
        return {"callsign": cs, "added": added, "status": "waiting",
                "aircraft": None, "route": None, "progress_pct": None,
                "eta_s": None, "eta_utc": None, "dist_to_dest_nm": None,
                "last_seen": None}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"follows": [
                {"callsign": f["callsign"], "added": f["added"]}
                for f in self._follows.values()]}))
        except OSError:
            log.exception("follows file write failed")

    # ---- REST surface -----------------------------------------------------

    def add(self, callsign: str) -> dict:
        cs = callsign.strip().upper()
        if not CALLSIGN_RE.fullmatch(cs):
            raise ValueError("callsign must be 3-8 letters/digits")
        if cs in self._follows:
            return self._follows[cs]
        if len(self._follows) >= config.MAX_FOLLOWS:
            raise TooManyFollows()
        self._follows[cs] = self._new_state(cs, int(time.time()))
        self._save()
        self.updated = int(time.time())
        # First data now, not a round-robin cycle from now
        asyncio.get_running_loop().create_task(self._poll_one_safe(cs))
        return self._follows[cs]

    def remove(self, callsign: str) -> bool:
        cs = callsign.strip().upper()
        if self._follows.pop(cs, None) is None:
            return False
        self._save()
        self.updated = int(time.time())
        return True

    def snapshot_now(self) -> dict:
        """Current follows with positions dead-reckoned to render time."""
        now = time.time()
        out = []
        for f in self._follows.values():
            f = dict(f)
            a = f.get("aircraft")
            if a and f.get("last_seen") and f["status"] == "live":
                age = now - f["last_seen"]
                if 0 <= age <= EXTRAP_MAX_S:
                    a = dead_reckon(a, age + (a.get("pos_age_s") or 0))
                    f["aircraft"] = a
                    self._derive_progress(f, a)
            out.append(f)
        return {"follows": out, "updated": self.updated,
                "max_follows": config.MAX_FOLLOWS}

    # ---- polling ----------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                self._expire()
                if self._rr == [] or not set(self._rr) <= set(self._follows):
                    self._rr = list(self._follows)
                if self._rr:
                    await self._poll_one_safe(self._rr.pop(0))
            except Exception:
                log.exception("follow poll failed")
            await asyncio.sleep(config.FOLLOW_POLL_SECONDS
                                if self._follows else 5)

    def _expire(self) -> None:
        now = time.time()
        for cs, f in list(self._follows.items()):
            landed_done = (f["status"] == "landed" and f.get("landed_at")
                           and now - f["landed_at"] > LANDED_REMOVE_S)
            if now - f["added"] > EXPIRE_S or landed_done:
                del self._follows[cs]
                self._save()
                self.updated = int(now)
                log.info("follow expired: %s", cs)

    async def _poll_one_safe(self, cs: str) -> None:
        try:
            await self._poll_one(cs)
        except Exception:
            log.exception("follow poll failed for %s", cs)

    async def _poll_one(self, cs: str) -> None:
        f = self._follows.get(cs)
        if f is None:
            return
        if config.DEMO_MODE:
            self._demo_fill(f)
            self.updated = int(time.time())
            return
        if radar.penalised(BUDGET):
            return
        await radar.throttle(BUDGET)
        resp = await self._client.get(CALLSIGN_URL.format(cs=cs), timeout=15)
        if resp.status_code in (403, 429):
            radar.penalise(BUDGET, resp.headers.get("Retry-After"))
        resp.raise_for_status()
        radar.clear_penalty(BUDGET)
        candidates = [ac for ac in (resp.json().get("ac") or [])
                      if ac.get("lat") is not None]
        now = int(time.time())
        if not candidates:
            if (f["status"] == "live" and f["last_seen"]
                    and now - f["last_seen"] > LOST_AFTER_S):
                f["status"] = "no_coverage"
                self.updated = now
            return
        # Duplicate callsigns exist; take the freshest position
        ac = min(candidates, key=lambda a: a.get("seen_pos") or 0)
        a = self._normalize(ac)
        await self._ensure_route(f)
        on_ground = ac.get("alt_baro") == "ground"
        f["aircraft"] = a
        f["last_seen"] = now
        self._derive_progress(f, a)
        if on_ground or (a.get("altitude_ft") or 99999) < 1500 and \
                (a.get("ground_speed_kt") or 999) < 80 and \
                (f.get("dist_to_dest_nm") or 999) < 50:
            if f["status"] != "landed":
                f["status"] = "landed"
                f["landed_at"] = now
        else:
            f["status"] = "live"
        self.updated = now

    def _normalize(self, ac: dict) -> dict:
        alt = ac.get("alt_baro")
        if not isinstance(alt, (int, float)):
            alt = ac.get("alt_geom")
        rate = ac.get("baro_rate", ac.get("geom_rate"))
        track = ac.get("track", ac.get("true_heading"))
        return {
            "hex": ac.get("hex"),
            "callsign": (ac.get("flight") or "").strip() or None,
            "registration": ac.get("r"),
            "type": ac.get("t"),
            "description": ac.get("desc"),
            "lat": ac.get("lat"),
            "lon": ac.get("lon"),
            "altitude_ft": alt if isinstance(alt, (int, float)) else None,
            "ground_speed_kt": ac.get("gs"),
            "track": track,
            "heading_cardinal": cardinal(track),
            "vertical_rate_fpm": rate if isinstance(rate, (int, float)) else None,
            "pos_age_s": ac.get("seen_pos")
                if isinstance(ac.get("seen_pos"), (int, float)) else None,
            "squawk": ac.get("squawk"),
        }

    async def _ensure_route(self, f: dict) -> None:
        if f["route"] is not None:
            return
        route = await self._meta.fetch_route(f["callsign"])
        if not route:
            return
        f["route"] = dict(route)
        for which, code_key in (("origin", "origin"), ("destination", "destination")):
            code = route.get(code_key)
            row = self._standing.airport_lookup(code) if code else None
            if row and row.get("lat") is not None:
                f["route"][f"{which}_lat"] = row["lat"]
                f["route"][f"{which}_lon"] = row["lon"]

    def _derive_progress(self, f: dict, a: dict) -> None:
        r = f.get("route") or {}
        if (r.get("origin_lat") is None or r.get("destination_lat") is None
                or a.get("lat") is None):
            return
        done = haversine_nm(r["origin_lat"], r["origin_lon"], a["lat"], a["lon"])
        rem = haversine_nm(a["lat"], a["lon"],
                           r["destination_lat"], r["destination_lon"])
        total = done + rem
        f["progress_pct"] = round(done / total * 100, 1) if total > 0 else None
        f["dist_to_dest_nm"] = round(rem, 1)
        gs = a.get("ground_speed_kt")
        if isinstance(gs, (int, float)) and gs > 50:
            f["eta_s"] = int(rem / gs * 3600)
            f["eta_utc"] = int(time.time()) + f["eta_s"]

    def _demo_fill(self, f: dict) -> None:
        """Fabricated mid-route flight so the view can be demoed offline."""
        o, d = DEMO_ORIGIN, DEMO_DEST
        total_nm = haversine_nm(o[2], o[3], d[2], d[3])
        frac = min(0.999, DEMO_START_FRAC
                   + (time.time() - f["added"]) * DEMO_GS / 3600.0 / total_nm)
        lat, lon = _gc_point(o[2], o[3], d[2], d[3], frac)
        ahead = _gc_point(o[2], o[3], d[2], d[3], min(1.0, frac + 0.001))
        track = bearing_deg(lat, lon, ahead[0], ahead[1])
        f["route"] = {"origin": o[0], "origin_name": o[1],
                      "destination": d[0], "destination_name": d[1],
                      "airline": "Singapore Airlines", "airline_iata": "SQ",
                      "origin_lat": o[2], "origin_lon": o[3],
                      "destination_lat": d[2], "destination_lon": d[3]}
        f["aircraft"] = {
            "hex": "demfol", "callsign": f["callsign"], "registration": "9V-SMF",
            "type": "A359", "description": "AIRBUS A350-900",
            "lat": round(lat, 4), "lon": round(lon, 4),
            "altitude_ft": 38000, "ground_speed_kt": DEMO_GS,
            "track": round(track, 1), "heading_cardinal": cardinal(track),
            "vertical_rate_fpm": 0, "pos_age_s": 0, "squawk": None,
        }
        f["status"] = "live"
        f["last_seen"] = int(time.time())
        self._derive_progress(f, f["aircraft"])
