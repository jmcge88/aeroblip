"""Background poller: tracks aircraft overhead and enriches them with routes."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime

from app import config
from app.providers.radar import RadarProvider

log = logging.getLogger(__name__)

CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

ROUTE_RETRY_SECONDS = 3600  # re-attempt unknown routes at most hourly

# Scripted flights for DEMO_MODE: each flies a straight track past home on a
# repeating cycle. offset_nm is the closest-approach distance, so the first
# two periodically enter the overhead ring and trigger the spotlight view.
DEMO_FLIGHTS = [
    {"callsign": "QFA551", "airline": "Qantas", "iata": "QF", "type": "B738",
     "desc": "BOEING 737-800", "manufacturer": "Boeing", "model": "737-838",
     "reg": "VH-VZR", "origin": ("SYD", "Sydney"), "dest": ("BNE", "Brisbane"),
     "alt": 6500, "gs": 285, "vr": -1400, "track": 15,
     "offset_nm": 0.8, "leg_nm": 45, "cycle": 300, "start": 0.0},
    {"callsign": "VOZ923", "airline": "Virgin Australia", "iata": "VA", "type": "B738",
     "desc": "BOEING 737-800", "manufacturer": "Boeing", "model": "737-8FE",
     "reg": "VH-YIR", "origin": ("BNE", "Brisbane"), "dest": ("MEL", "Melbourne"),
     "alt": 11000, "gs": 320, "vr": 1600, "track": 205,
     "offset_nm": 2.5, "leg_nm": 45, "cycle": 300, "start": 0.5},
    {"callsign": "JST812", "airline": "Jetstar", "iata": "JQ", "type": "A320",
     "desc": "AIRBUS A320", "manufacturer": "Airbus", "model": "A320-232",
     "reg": "VH-VGF", "origin": ("OOL", "Gold Coast"), "dest": ("CNS", "Cairns"),
     "alt": 34000, "gs": 445, "vr": 0, "track": 330,
     "offset_nm": 14, "leg_nm": 55, "cycle": 420, "start": 0.25},
    {"callsign": "UAE430", "airline": "Emirates", "iata": "EK", "type": "A388",
     "desc": "AIRBUS A380-800", "manufacturer": "Airbus", "model": "A380-861",
     "reg": "A6-EOP", "origin": ("DXB", "Dubai"), "dest": ("BNE", "Brisbane"),
     "alt": 22000, "gs": 390, "vr": -900, "track": 95,
     "offset_nm": 24, "leg_nm": 55, "cycle": 480, "start": 0.65},
    {"callsign": "ANZ146", "airline": "Air New Zealand", "iata": "NZ", "type": "A21N",
     "desc": "AIRBUS A321 NEO", "manufacturer": "Airbus", "model": "A321-271NX",
     "reg": "ZK-NNE", "origin": ("BNE", "Brisbane"), "dest": ("AKL", "Auckland"),
     "alt": 28000, "gs": 430, "vr": 800, "track": 120,
     "offset_nm": -19, "leg_nm": 55, "cycle": 420, "start": 0.85},
]

# One-shot flight spawned by the demo "simulate flyover" button. Moves
# demo-fast so the button feels responsive: overhead in ~25 s, gone in ~2 min.
DEMO_FLYOVER = {
    "callsign": "SIA245", "airline": "Singapore Airlines", "iata": "SQ", "type": "A359",
    "desc": "AIRBUS A350-900", "manufacturer": "Airbus", "model": "A350-941",
    "reg": "9V-SMF", "origin": ("SIN", "Singapore"), "dest": ("BNE", "Brisbane"),
    "alt": 5200, "gs": 265, "vr": -1100, "track": 75, "offset_nm": 0.4,
}
FLYOVER_START_NM = -6.0
FLYOVER_END_NM = 25.0
FLYOVER_SPEED_NM_S = 0.25


def is_airline_callsign(callsign: str) -> bool:
    """True for ICAO airline callsigns: 3 letters then a flight number (QFA551).

    Registrations and GA callsigns (VHABC, N123AB, 7788) never have a route to
    look up, and around a busy airport they are the majority of the traffic -
    around three quarters of it at YBBN. Asking a paid provider about them buys
    nothing but 404s, so both route and airline lookups are gated on this.
    """
    return (len(callsign) > 3 and callsign[:3].isalpha()
            and callsign[3].isdigit())


def cardinal(track: float | None) -> str | None:
    if track is None:
        return None
    return CARDINALS[round(track / 22.5) % 16]


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


class OverheadPoller:
    """Polls one location. The env config supplies the defaults (the web
    dashboard's location); device fleets get one poller per distinct location
    via services.hub, each carrying its own coordinates and radii."""

    def __init__(self, provider: RadarProvider, meta, board_cache=None, *,
                 lat: float | None = None, lon: float | None = None,
                 overhead_nm: float | None = None, area_nm: float | None = None,
                 airport_iata: str | None = None, airport_name: str | None = None):
        self._provider = provider
        self._meta = meta  # AdsbdbMeta or StandingDataMeta (routes/airframes/airlines)
        self._board = board_cache  # BoardCache; used to correct stale route data
        self._lat = config.HOME_LAT if lat is None else lat
        self._lon = config.HOME_LON if lon is None else lon
        self._overhead_nm = config.OVERHEAD_RADIUS_NM if overhead_nm is None else overhead_nm
        self._area_nm = config.AREA_RADIUS_NM if area_nm is None else area_nm
        self._airport_iata = airport_iata or config.AIRPORT_IATA
        self._airport_name = airport_name or (config.AIRPORT_NAME if airport_iata is None
                                              else airport_iata)
        self.last_used = time.monotonic()  # idle-reaping (see services.hub)
        self._board_index: dict[str, list[dict]] = {}
        self._board_index_key: int | None = None
        self.snapshot: dict = {"aircraft": [], "updated": None, "provider": None,
                               "overhead_count": 0,
                               "overhead_radius_nm": self._overhead_nm,
                               "area_radius_nm": self._area_nm}
        self._route_cache: dict[str, dict | None] = {}
        self._route_attempted: dict[str, float] = {}
        self._airline_cache: dict[str, dict | None] = {}
        self._info_cache: dict[str, dict | None] = {}
        self._flyovers: list[float] = []  # start times of demo flyovers
        self._listeners: set[asyncio.Queue] = set()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def busy(self) -> bool:
        """True while a websocket client is subscribed (blocks idle-reaping)."""
        return bool(self._listeners)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    def _notify(self) -> None:
        for q in self._listeners:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(self.snapshot)

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._poll_once()
            except Exception:
                log.exception("overhead poll failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, config.POLL_SECONDS - elapsed))

    async def _poll_once(self) -> None:
        if config.DEMO_MODE:
            aircraft = self._demo_aircraft()
            provider = "demo"
        else:
            raw = await self._provider.fetch_point(self._lat, self._lon, self._area_nm)
            aircraft = [self._normalize(ac) for ac in raw]
            aircraft = [a for a in aircraft if a is not None]
            await self._enrich_routes(aircraft)
            await self._enrich_airlines(aircraft)
            self._apply_board_routes(aircraft)
            self._drop_implausible_routes(aircraft)
            await self._enrich_info(aircraft)
            provider = self._provider.active
        aircraft.sort(key=lambda a: a["distance_nm"] if a["distance_nm"] is not None else 999)
        self.snapshot = {
            "aircraft": aircraft,
            "updated": int(time.time()),
            "provider": provider,
            "overhead_count": sum(1 for a in aircraft if a["overhead"]),
            "overhead_radius_nm": self._overhead_nm,
            "area_radius_nm": self._area_nm,
        }
        self._notify()

    def trigger_flyover(self) -> None:
        """Demo mode: spawn a one-shot flight that passes directly overhead."""
        self._flyovers.append(time.time())

    async def poll_now(self) -> None:
        await self._poll_once()

    def _demo_aircraft(self) -> list[dict]:
        """Fabricated moving traffic for DEMO_MODE (no network calls)."""
        now = time.time()
        out = []
        for i, f in enumerate(DEMO_FLIGHTS):
            t = (now / f["cycle"] + f["start"]) % 1.0
            along = (t * 2 - 1) * f["leg_nm"]  # NM before/after closest approach
            ac = self._demo_ac(f, along, f"dem{i:03d}")
            if ac:
                out.append(ac)
        active = []
        for j, t0 in enumerate(self._flyovers):
            along = FLYOVER_START_NM + (now - t0) * FLYOVER_SPEED_NM_S
            if along <= FLYOVER_END_NM:
                active.append(t0)
                ac = self._demo_ac(DEMO_FLYOVER, along, f"fly{j:03d}")
                if ac:
                    out.append(ac)
        self._flyovers = active
        return out

    def _demo_ac(self, f: dict, along: float, hexcode: str) -> dict | None:
        th = math.radians(f["track"])
        dx, dy = math.sin(th), math.cos(th)         # unit vector along track (E, N)
        x = along * dx + f["offset_nm"] * dy        # perpendicular offset to the right
        y = along * dy - f["offset_nm"] * dx
        dist = math.hypot(x, y)
        if dist > self._area_nm:
            return None
        bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
        rate = f["vr"]
        return {
            "hex": hexcode,
            "callsign": f["callsign"],
            "registration": f["reg"],
            "type": f["type"],
            "description": f["desc"],
            "lat": round(self._lat + y / 60.0, 5),
            "lon": round(self._lon
                         + x / (60.0 * math.cos(math.radians(self._lat))), 5),
            "altitude_ft": f["alt"],
            "ground_speed_kt": f["gs"],
            "track": f["track"],
            "heading_cardinal": cardinal(f["track"]),
            "vertical_rate_fpm": rate,
            "phase": "climbing" if rate > 300 else "descending" if rate < -300 else "level",
            "distance_nm": round(dist, 2),
            "bearing_from_home": round(bearing, 1),
            "overhead": dist <= self._overhead_nm,
            "route": {"origin": f["origin"][0], "origin_name": f["origin"][1],
                      "destination": f["dest"][0], "destination_name": f["dest"][1],
                      "airline": f["airline"], "airline_iata": f["iata"]},
            "airline": {"airline": f["airline"], "airline_iata": f["iata"]},
            "info": {"manufacturer": f["manufacturer"], "model": f["model"],
                     "owner": f["airline"], "country": "Australia",
                     "photo": None, "photo_thumb": None},
        }

    def _normalize(self, ac: dict) -> dict | None:
        alt = ac.get("alt_baro")
        if alt == "ground":
            return None
        if not isinstance(alt, (int, float)):
            alt = ac.get("alt_geom")
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            return None
        if ac.get("t") in ("TWR", "GND"):  # airport ground stations
            return None
        # Require a plausible altitude: filters parked aircraft, ground
        # vehicles and MLAT ghost targets near the airport.
        if not isinstance(alt, (int, float)) or alt < 500:
            return None

        dist = ac.get("dst")
        if dist is None:
            dist = round(haversine_nm(self._lat, self._lon, lat, lon), 2)
        rate = ac.get("baro_rate", ac.get("geom_rate"))
        phase = None
        if isinstance(rate, (int, float)):
            phase = "climbing" if rate > 300 else "descending" if rate < -300 else "level"
        track = ac.get("track", ac.get("true_heading"))
        callsign = (ac.get("flight") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{2,8}", callsign):
            callsign = ""  # transponders sometimes broadcast garbage
        return {
            "hex": ac.get("hex"),
            "callsign": callsign or None,
            "registration": ac.get("r"),
            "type": ac.get("t"),
            "description": ac.get("desc"),
            "lat": lat,
            "lon": lon,
            "altitude_ft": alt if isinstance(alt, (int, float)) else None,
            "ground_speed_kt": ac.get("gs"),
            "track": track,
            "heading_cardinal": cardinal(track),
            "vertical_rate_fpm": rate if isinstance(rate, (int, float)) else None,
            "phase": phase,
            "distance_nm": dist,
            "bearing_from_home": ac.get("dir"),
            "squawk": ac.get("squawk"),
            "emergency": ac.get("emergency"),
            "overhead": dist is not None and dist <= self._overhead_nm,
            "route": None,
            "airline": None,
            "info": None,
        }

    async def _airline_for(self, callsign: str) -> dict | None:
        """Airline info for a callsign: from its route if known, else by
        3-letter ICAO prefix (e.g. QLK1258 -> QLK -> QantasLink)."""
        route = self._route_cache.get(callsign)
        if route:
            return {"airline": route["airline"], "airline_iata": route["airline_iata"]}
        if not is_airline_callsign(callsign):
            return None
        prefix = callsign[:3]
        if prefix not in self._airline_cache:
            self._airline_cache[prefix] = await self._meta.fetch_airline(prefix)
        return self._airline_cache[prefix]

    async def _enrich_routes(self, aircraft: list[dict]) -> None:
        now = time.time()
        lookups: list[str] = []
        for a in aircraft:
            cs = a["callsign"]
            if not cs or not is_airline_callsign(cs):
                continue  # GA/registration callsigns have no route to find
            if cs in self._route_cache:
                a["route"] = self._route_cache[cs]
            elif now - self._route_attempted.get(cs, 0) > ROUTE_RETRY_SECONDS:
                lookups.append(cs)

        if not lookups:
            return
        for cs in lookups:
            self._route_attempted[cs] = now
        results = await asyncio.gather(
            *(self._meta.fetch_route(cs) for cs in lookups)
        )
        routes = dict(zip(lookups, results))
        for cs, route in routes.items():
            self._route_cache[cs] = route
        for a in aircraft:
            if a["callsign"] in routes:
                a["route"] = routes[a["callsign"]]
        # keep caches bounded
        if len(self._route_cache) > 2000:
            self._route_cache.clear()
            self._route_attempted.clear()

    async def _enrich_airlines(self, aircraft: list[dict]) -> None:
        for a in aircraft:
            if a["callsign"]:
                a["airline"] = await self._airline_for(a["callsign"])

    # ---- board cross-reference -------------------------------------------
    # The callsign->route databases are static and sometimes stale. The
    # airport board (AeroDataBox) is live, so when a nearby aircraft matches
    # a flight on today's board, the board's route wins.

    def _refresh_board_index(self) -> None:
        snap = self._board.snapshot
        if snap["updated"] == self._board_index_key:
            return
        index: dict[str, list[dict]] = {}
        for row in snap["arrivals"] + snap["departures"]:
            m = re.fullmatch(r"([A-Z0-9]{2})0*(\d{1,4})", row.get("flight") or "")
            if m:
                index.setdefault(m.group(1) + m.group(2), []).append(row)
        self._board_index = index
        self._board_index_key = snap["updated"]

    @staticmethod
    def _row_nearest_now(rows: list[dict]) -> dict:
        """Same flight number can appear as both arrival and departure;
        pick the movement scheduled closest to now."""
        now = datetime.now().astimezone()

        def gap(row: dict) -> float:
            try:
                sched = datetime.fromisoformat(row["estimated"] or row["scheduled"])
                return abs((sched - now).total_seconds())
            except (TypeError, ValueError):
                return float("inf")

        return min(rows, key=gap)

    def _apply_board_routes(self, aircraft: list[dict]) -> None:
        if self._board is None or self._board.snapshot.get("mock"):
            return
        self._refresh_board_index()
        if not self._board_index:
            return
        home = (self._airport_iata, self._airport_name)
        for a in aircraft:
            cs = a["callsign"] or ""
            m = re.fullmatch(r"([A-Z]{3})0*(\d{1,4})[A-Z]?", cs)
            iata = (a.get("airline") or {}).get("airline_iata")
            if not m or not iata:
                continue
            rows = self._board_index.get(f"{iata}{m.group(2)}")
            if not rows:
                continue
            row = self._row_nearest_now(rows)
            other = (row["code"], row["city"])
            origin, dest = (home, other) if row["direction"] == "departure" else (other, home)
            a["route"] = {
                "origin": origin[0], "origin_name": origin[1],
                "destination": dest[0], "destination_name": dest[1],
                "airline": row["airline"] or (a["airline"] or {}).get("airline"),
                "airline_iata": iata,
            }

    def _drop_implausible_routes(self, aircraft: list[dict]) -> None:
        """Suppress stale cached routes the board couldn't correct.

        Some carriers' callsigns aren't flight numbers (QTR16M is not QR16),
        so neither adsbdb nor the board cross-reference can name their route
        reliably. When such an aircraft is low, close, and climbing or
        descending - i.e. obviously departing from or arriving at OUR airport
        - a route naming two other cities is wrong: show the airline alone
        rather than a confidently wrong city pair. High overflights keep
        their route; we can't cheaply disprove those.
        """
        for a in aircraft:
            r = a.get("route")
            if not r or self._airport_iata in (r.get("origin"), r.get("destination")):
                continue
            alt, vr = a.get("altitude_ft"), a.get("vertical_rate_fpm")
            dist = a.get("distance_nm")
            if alt is None or vr is None or dist is None:
                continue
            # A strong climb below cruise inside the area = a local departure;
            # descents get a tighter box since arrivals descend from far out.
            departing = vr > 1000 and alt < 28000
            arriving = vr < -300 and alt < 20000 and dist < 50
            if departing or arriving:
                a["route"] = None

    async def _enrich_info(self, aircraft: list[dict]) -> None:
        """Airframe details (incl. photo) for overhead aircraft and the nearest
        few that appear as cards in the nearby-traffic view."""
        for i, a in enumerate(aircraft):
            hexcode = a["hex"]
            if not hexcode or (not a["overhead"] and i >= 6):
                continue
            if hexcode not in self._info_cache:
                self._info_cache[hexcode] = await self._meta.fetch_aircraft(hexcode)
                if len(self._info_cache) > 2000:
                    self._info_cache.clear()
            a["info"] = self._info_cache[hexcode]
