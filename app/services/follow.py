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

Follows are namespaced per caller ("owner" - the device token, or "default"
for an unauthenticated caller when REQUIRE_DEVICE_TOKEN is off): each owner
manages and sees only their own follows, up to MAX_FOLLOWS each - it's a
per-owner limit, not a fleet-wide total. The shared round-robin poll loop
doesn't care about ownership at all; it just walks every (owner, callsign)
pair on the same adsb.lol throttle budget everything else shares. A follow
added before this existed has no owner on disk and is treated as "default".
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import time
from pathlib import Path

import httpx

from app import config
from app.providers import radar
from app.services import notify
from app.services.poller import bearing_deg, cardinal, dead_reckon, haversine_nm

log = logging.getLogger(__name__)

CALLSIGN_URL = "https://api.adsb.lol/v2/callsign/{cs}"
BUDGET = "adsblol"  # shares app.providers.radar's global throttle/penalties
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,8}$")
# Two letters then digits looks exactly like an IATA flight number (JQ59) -
# the single most common way to add a follow that can never resolve, since
# adsb.lol's callsign endpoint only matches the ICAO-prefixed form (JST59)
# a transponder actually broadcasts. See _normalise_iata_prefix below.
_IATA_CALLSIGN_RE = re.compile(r"^([A-Z]{2})(\d[0-9A-Z]*)$")
DEFAULT_OWNER = "default"
EXPIRE_S = 24 * 3600
LOST_AFTER_S = 600       # live -> no_coverage after this long without a fix
LANDED_REMOVE_S = 1800   # landed follows clean themselves up
EXTRAP_MAX_S = 150.0     # round-robin polls are slow; reckon a bit further

# Follow alerts: the things worth interrupting someone's day for. Holding
# reuses the poller's circling idea on the follow's own (60 s) samples: a
# standard racetrack turns ~360deg every 4 min, so 450deg inside the window is
# unambiguous. ETA drift compares against the FIRST estimate ever made for
# the flight and re-alerts per further full increment, not per wobble.
HOLD_WINDOW_S = 900.0
HOLD_MIN_TURN_DEG = 450.0
HOLD_MIN_SAMPLES = 6
HOLD_REALERT_S = 1800
ETA_DRIFT_S = 45 * 60           # "running late" threshold and re-alert step
DIVERT_ALT_FT = 12000           # descending below this ...
DIVERT_FPM = -400               # ... at at least this rate ...
DIVERT_MIN_DIST_NM = 150.0      # ... this far from the destination
LANDED_AWAY_NM = 80.0           # touchdown further out than this = diverted
MAX_EVENTS = 6                  # kept per follow, embedded in snapshots

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
        self._follows: dict[str, dict[str, dict]] = {}  # owner -> callsign -> state
        self._rr: list[tuple[str, str]] = []  # round-robin queue of (owner, callsign)
        # (owner, callsign) -> untried ICAO candidates, for an ambiguous IATA
        # code being disambiguated by live traffic - see _try_next_candidate.
        self._iata_candidates: dict[tuple[str, str], list[str]] = {}
        self.updated: int | None = None
        try:
            if self._path.exists():
                for f in json.loads(self._path.read_text())["follows"]:
                    if CALLSIGN_RE.fullmatch(f.get("callsign", "")):
                        # Self-heal a follow saved before _normalise_iata_prefix
                        # existed - no reason to make someone re-add it. A
                        # follow saved before per-owner isolation existed has
                        # no "owner" field either; it lands in DEFAULT_OWNER.
                        cs = self._normalise_iata_prefix(f["callsign"])
                        owner = f.get("owner") or DEFAULT_OWNER
                        self._follows.setdefault(owner, {})[cs] = self._new_state(
                            cs, f.get("added") or int(time.time()))
        except (OSError, ValueError, KeyError):
            log.exception("follows file unreadable - starting empty")

    @staticmethod
    def _new_state(cs: str, added: int) -> dict:
        # Keys starting with "_" are working state, stripped from snapshots.
        return {"callsign": cs, "added": added, "status": "waiting",
                "aircraft": None, "route": None, "progress_pct": None,
                "eta_s": None, "eta_utc": None, "dist_to_dest_nm": None,
                "last_seen": None, "holding": False, "events": []}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"follows": [
                {"owner": owner, "callsign": f["callsign"], "added": f["added"]}
                for owner, follows in self._follows.items()
                for f in follows.values()]}))
        except OSError:
            log.exception("follows file write failed")

    # ---- REST surface -----------------------------------------------------

    def _normalise_iata_prefix(self, cs: str) -> str:
        """JQ59 (IATA - what a human typed, copying it off Google/an airport
        board) never matches anything on adsb.lol; JST59 (ICAO - what the
        transponder broadcasts) does. Same "pilot entered the IATA form"
        translation the route lookup already does, reusing the same
        standing-data airlines table - see standing_data.py's fetch_route.
        A miss (unknown airline, standing-data not ready yet) just means the
        callsign is used as typed, same as before this existed."""
        m = _IATA_CALLSIGN_RE.match(cs)
        if not m:
            return cs
        icao = self._standing.airline_icao_for_iata(m.group(1))
        return icao + m.group(2) if icao else cs

    def add(self, owner: str, callsign: str) -> dict:
        cs = callsign.strip().upper()
        if not CALLSIGN_RE.fullmatch(cs):
            raise ValueError("callsign must be 3-8 letters/digits")
        cs = self._normalise_iata_prefix(cs)
        owned = self._follows.setdefault(owner, {})
        if cs in owned:
            return owned[cs]
        if len(owned) >= config.MAX_FOLLOWS:
            raise TooManyFollows()
        owned[cs] = self._new_state(cs, int(time.time()))
        self._save()
        self.updated = int(time.time())
        # First data now, not a round-robin cycle from now
        asyncio.get_running_loop().create_task(self._poll_one_safe(owner, cs))
        return owned[cs]

    def remove(self, owner: str, callsign: str) -> bool:
        cs = callsign.strip().upper()
        if self._follows.get(owner, {}).pop(cs, None) is None:
            return False
        if not self._follows[owner]:
            del self._follows[owner]
        self._iata_candidates.pop((owner, cs), None)
        self._save()
        self.updated = int(time.time())
        return True

    def snapshot_now(self, owner: str) -> dict:
        """This owner's follows, with positions dead-reckoned to render time."""
        now = time.time()
        out = []
        for f in self._follows.get(owner, {}).values():
            f = {k: v for k, v in f.items() if not k.startswith("_")}
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

    def _all_pairs(self) -> list[tuple[str, str]]:
        return [(owner, cs) for owner, follows in self._follows.items() for cs in follows]

    async def run(self) -> None:
        while True:
            try:
                self._expire()
                pairs = self._all_pairs()
                if self._rr == [] or not set(self._rr) <= set(pairs):
                    self._rr = pairs
                if self._rr:
                    await self._poll_one_safe(*self._rr.pop(0))
            except Exception:
                log.exception("follow poll failed")
            await asyncio.sleep(config.FOLLOW_POLL_SECONDS
                                if self._follows else 5)

    def _expire(self) -> None:
        now = time.time()
        any_expired = False
        for owner, follows in list(self._follows.items()):
            for cs, f in list(follows.items()):
                landed_done = (f["status"] == "landed" and f.get("landed_at")
                               and now - f["landed_at"] > LANDED_REMOVE_S)
                if now - f["added"] > EXPIRE_S or landed_done:
                    del follows[cs]
                    self._iata_candidates.pop((owner, cs), None)
                    any_expired = True
                    log.info("follow expired: %s (owner=%s)", cs, owner)
            if not follows:
                del self._follows[owner]
        if any_expired:
            self.updated = int(now)
            self._save()

    async def _poll_one_safe(self, owner: str, cs: str) -> None:
        try:
            await self._poll_one(owner, cs)
        except Exception:
            log.exception("follow poll failed for %s (owner=%s)", cs, owner)

    async def _poll_one(self, owner: str, cs: str) -> None:
        f = self._follows.get(owner, {}).get(cs)
        if f is None:
            return
        if config.DEMO_MODE:
            self._demo_fill(f)
            self.updated = int(time.time())
            return
        # Route/airline are knowable independently of live position - a
        # follow added for a flight that's already landed, hasn't departed
        # yet, or is crossing an oceanic coverage gap should still be able
        # to show its route, so this no longer waits for a position fix.
        await self._ensure_route(f)
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
            if await self._try_next_candidate(owner, cs, f):
                return  # renamed to the resolved callsign; picked up next cycle
            if (f["status"] == "live" and f["last_seen"]
                    and now - f["last_seen"] > LOST_AFTER_S):
                f["status"] = "no_coverage"
            self.updated = now  # a route may have just resolved even with no fix
            return
        # Duplicate callsigns exist; take the freshest position
        ac = min(candidates, key=lambda a: a.get("seen_pos") or 0)
        a = self._normalize(ac)
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
                self._landed_event(f)
        else:
            f["status"] = "live"
            self._check_alerts(f, a, now)
        self.updated = now

    def _event(self, f: dict, kind: str, title: str, message: str,
               priority: str = "default") -> None:
        """Record a follow event (rides the follow snapshot to dashboards for
        toasts/voice) and push it to the phone. Fire-and-forget, same as watch
        matches - a slow push service must never stall the poll loop."""
        ev = {"id": secrets.token_urlsafe(6), "ts": int(time.time()),
              "kind": kind, "callsign": f["callsign"],
              "title": title, "message": message}
        f.setdefault("events", []).append(ev)
        del f["events"][:-MAX_EVENTS]
        log.info("follow event [%s]: %s - %s", kind, title, message)
        asyncio.get_running_loop().create_task(
            notify.push(self._client, title, message, priority=priority, event=ev))

    def _landed_event(self, f: dict) -> None:
        cs = f["callsign"]
        r = f.get("route") or {}
        dest = r.get("destination_name") or r.get("destination")
        dist = f.get("dist_to_dest_nm")
        if dist is not None and dist > LANDED_AWAY_NM:
            self._event(f, "diverted", f"{cs} landed away from destination",
                        f"On the ground {dist:.0f} NM from "
                        f"{dest or 'its destination'}.", priority="high")
        else:
            self._event(f, "landed",
                        f"{cs} has landed" + (f" in {dest}" if dest else ""),
                        "Touchdown detected.")

    def _check_alerts(self, f: dict, a: dict, now: int) -> None:
        """In-flight alerts: holding patterns, a descent nowhere near the
        destination (the classic diversion signature), and ETA drift."""
        cs = f["callsign"]
        r = f.get("route") or {}
        dest = r.get("destination_name") or r.get("destination")
        dist = f.get("dist_to_dest_nm")
        track = a.get("track")
        if track is not None:
            hist = f.setdefault("_hdg", [])
            hist.append((now, track))
            while hist and now - hist[0][0] > HOLD_WINDOW_S:
                hist.pop(0)
            turn = sum(((t1 - t0 + 540) % 360) - 180
                       for (_, t0), (_, t1) in zip(hist, hist[1:]))
            f["holding"] = (len(hist) >= HOLD_MIN_SAMPLES
                            and abs(turn) >= HOLD_MIN_TURN_DEG)
            if f["holding"] and now - f.get("_hold_alerted", 0) > HOLD_REALERT_S:
                f["_hold_alerted"] = now
                where = (f" {dist:.0f} NM from {dest}"
                         if dist is not None and dest else "")
                self._event(f, "holding", f"{cs} is holding",
                            f"Flying circles{where} - expect a delay.")
        alt, vr = a.get("altitude_ft"), a.get("vertical_rate_fpm")
        if (not f.get("_descent_alerted") and alt is not None and vr is not None
                and dist is not None and alt < DIVERT_ALT_FT
                and vr < DIVERT_FPM and dist > DIVERT_MIN_DIST_NM):
            f["_descent_alerted"] = True
            self._event(f, "descent", f"{cs} descending far from destination",
                        f"Down to {round(alt):,} ft, {dist:.0f} NM short of "
                        f"{dest or 'its destination'} - possible diversion.",
                        priority="high")
        if f.get("eta_utc"):
            if f.get("_eta0") is None:
                f["_eta0"] = f["eta_utc"]
            drift = f["eta_utc"] - f["_eta0"]
            if drift - f.get("_eta_alerted", 0) >= ETA_DRIFT_S:
                f["_eta_alerted"] = drift
                self._event(f, "late", f"{cs} is running late",
                            f"Now expected ~{round(drift / 60)} min later "
                            f"than first estimated.")

    async def _try_next_candidate(self, owner: str, cs: str, f: dict) -> bool:
        """The literal callsign found nothing, and its IATA prefix covers
        several real airlines (QF alone covers six) - _normalise_iata_prefix
        already declined to guess at add() time rather than risk querying
        the wrong one. Instead of guessing, find out: work through the real
        candidates one at a time, one extra throttled request per poll (this
        follow's normal one plus this), so disambiguation costs the same
        shared adsb.lol budget as any other follow, just takes a few more
        minutes to land. Renames the follow to the winning ICAO callsign as
        soon as one actually has live traffic - permanently, so every later
        poll (and the route lookup) goes straight to the right one.

        Returns True if a candidate was tried this call (whether or not it
        won) - the caller should stop rather than also touch self.updated,
        since a rename already means a state change happened.
        """
        m = _IATA_CALLSIGN_RE.match(cs)
        if not m:
            return False
        key = (owner, cs)
        if key not in self._iata_candidates:
            iata, number = m.group(1), m.group(2)
            icaos = self._standing.airline_icaos_for_iata(iata)
            queue = [icao + number for icao in icaos if icao + number != cs]
            if not queue:
                return False  # unknown IATA code - nothing to try
            self._iata_candidates[key] = queue
        queue = self._iata_candidates[key]
        candidate = queue.pop(0)
        if not queue:
            del self._iata_candidates[key]
        if radar.penalised(BUDGET):
            return True  # still "handled" - don't fall through to no_coverage logic
        await radar.throttle(BUDGET)
        try:
            resp = await self._client.get(CALLSIGN_URL.format(cs=candidate), timeout=15)
            if resp.status_code in (403, 429):
                radar.penalise(BUDGET, resp.headers.get("Retry-After"))
            resp.raise_for_status()
            radar.clear_penalty(BUDGET)
            hits = [ac for ac in (resp.json().get("ac") or []) if ac.get("lat") is not None]
        except Exception as exc:
            log.warning("candidate callsign check failed for %s: %s", candidate, exc)
            return True
        if not hits:
            self.updated = int(time.time())
            return True
        owned = self._follows.get(owner, {})
        if cs in owned:
            state = owned.pop(cs)
            state["callsign"] = candidate
            owned[candidate] = state
            self._iata_candidates.pop(key, None)
            self._save()
            self.updated = int(time.time())
            log.info("follow %s (owner=%s) resolved to %s", cs, owner, candidate)
        return True

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
