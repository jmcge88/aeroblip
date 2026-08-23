"""ISS pass predictions - because it's also flying over your house.

Orbital elements (TLE) come from CelesTrak (freely redistributable), cached
on disk and refreshed twice a day; propagation is done locally with sgp4.
A pass is reported "visible" when the station is above PASS_MIN_ELEVATION_DEG,
sunlit, and the observer is in twilight or darker - the classic naked-eye
sighting condition. Everything is computed on demand and cached per ~half-
degree cell, so a fleet of dashboards costs a couple of TLE downloads a day.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path

import httpx

from app.services.sun import NIGHT_ELEVATION_DEG, sun_eci_unit, sun_position

log = logging.getLogger(__name__)

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
TLE_REFRESH_S = 12 * 3600
PASS_MIN_ELEVATION_DEG = 10.0
SEARCH_HOURS = 48
STEP_S = 30
RESULT_TTL_S = 6 * 3600
MAX_PASSES = 8

EARTH_RADIUS_KM = 6371.0
WGS84_A = 6378.137
WGS84_E2 = 6.69437999014e-3

CARDINALS8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _cardinal8(az: float) -> str:
    return CARDINALS8[round(az / 45.0) % 8]


def _gmst_deg(ts: float) -> float:
    d = ts / 86400.0 - 10957.5  # days since J2000.0 (same base as sun.py)
    return ((18.697374558 + 24.06570982441908 * d) % 24.0) * 15.0


def _observer_ecef(lat: float, lon: float) -> tuple[float, float, float]:
    p, l = math.radians(lat), math.radians(lon)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(p) ** 2)
    return (n * math.cos(p) * math.cos(l),
            n * math.cos(p) * math.sin(l),
            n * (1 - WGS84_E2) * math.sin(p))


def _look_angles(r_teme, ts: float, obs, lat: float, lon: float) -> tuple[float, float]:
    """(elevation_deg, azimuth_deg) of a TEME position from an observer."""
    th = math.radians(_gmst_deg(ts))
    x = r_teme[0] * math.cos(th) + r_teme[1] * math.sin(th)
    y = -r_teme[0] * math.sin(th) + r_teme[1] * math.cos(th)
    z = r_teme[2]
    dx, dy, dz = x - obs[0], y - obs[1], z - obs[2]
    p, l = math.radians(lat), math.radians(lon)
    e = -math.sin(l) * dx + math.cos(l) * dy
    n = (-math.sin(p) * math.cos(l) * dx - math.sin(p) * math.sin(l) * dy
         + math.cos(p) * dz)
    u = (math.cos(p) * math.cos(l) * dx + math.cos(p) * math.sin(l) * dy
         + math.sin(p) * dz)
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return (math.degrees(math.asin(u / rng)),
            (math.degrees(math.atan2(e, n)) + 360.0) % 360.0)


def _sunlit(r_teme, ts: float) -> bool:
    """Cylindrical earth-shadow model: fine for a yes/no sighting call."""
    s = sun_eci_unit(ts)
    proj = r_teme[0] * s[0] + r_teme[1] * s[1] + r_teme[2] * s[2]
    if proj >= 0:
        return True  # on the sunny side
    r2 = r_teme[0] ** 2 + r_teme[1] ** 2 + r_teme[2] ** 2
    return math.sqrt(r2 - proj * proj) > EARTH_RADIUS_KM


def _compute_passes(l1: str, l2: str, lat: float, lon: float,
                    start_ts: float) -> list[dict]:
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(l1, l2)
    obs = _observer_ecef(lat, lon)
    passes: list[dict] = []
    current: dict | None = None
    steps = int(SEARCH_HOURS * 3600 / STEP_S)
    for i in range(steps):
        ts = start_ts + i * STEP_S
        jd = 2440587.5 + ts / 86400.0
        jd_whole = math.floor(jd - 0.5) + 0.5  # astronomical days start at noon
        err, r, _ = sat.sgp4(jd_whole, jd - jd_whole)
        if err != 0:
            continue
        el, az = _look_angles(r, ts, obs, lat, lon)
        if el >= PASS_MIN_ELEVATION_DEG:
            visible_now = (_sunlit(r, ts)
                           and sun_position(lat, lon, ts)[0] < NIGHT_ELEVATION_DEG)
            if current is None:
                current = {"start": int(ts), "start_dir": _cardinal8(az),
                           "max_elevation_deg": el, "visible": visible_now}
            else:
                current["max_elevation_deg"] = max(current["max_elevation_deg"], el)
                current["visible"] = current["visible"] or visible_now
            current["end"] = int(ts)
            current["end_dir"] = _cardinal8(az)
        elif current is not None:
            current["max_elevation_deg"] = round(current["max_elevation_deg"])
            passes.append(current)
            current = None
            if len(passes) >= MAX_PASSES:
                break
    return passes


class IssTracker:
    def __init__(self, client: httpx.AsyncClient, cache_path: str | Path):
        self._client = client
        self._tle_path = Path(cache_path)
        self._results: dict[tuple, tuple[float, dict]] = {}

    async def _tle(self) -> tuple[str, str] | None:
        """TLE lines from the disk cache, refreshed from CelesTrak when old.
        A failed refresh falls back to the stale file - the ISS's orbit drifts
        slowly enough that day-old elements still predict passes to the minute."""
        fresh = (self._tle_path.exists()
                 and time.time() - self._tle_path.stat().st_mtime < TLE_REFRESH_S)
        if not fresh:
            try:
                resp = await self._client.get(TLE_URL, timeout=20, follow_redirects=True)
                resp.raise_for_status()
                if "\n1 " in "\n" + resp.text.strip():
                    self._tle_path.parent.mkdir(parents=True, exist_ok=True)
                    self._tle_path.write_text(resp.text)
            except Exception as exc:
                log.warning("ISS TLE refresh failed: %s", exc)
        try:
            lines = [ln.strip() for ln in self._tle_path.read_text().splitlines()
                     if ln.strip()]
            l1 = next(ln for ln in lines if ln.startswith("1 "))
            l2 = next(ln for ln in lines if ln.startswith("2 "))
            return l1, l2
        except (OSError, StopIteration):
            return None

    async def passes_for(self, lat: float, lon: float) -> dict:
        key = (round(lat * 2) / 2, round(lon * 2) / 2)
        cached = self._results.get(key)
        now = time.time()
        if cached and now - cached[0] < RESULT_TTL_S:
            return cached[1]
        tle = await self._tle()
        if tle is None:
            return {"passes": [], "error": "no orbital elements"}
        passes = await asyncio.to_thread(_compute_passes, tle[0], tle[1],
                                         lat, lon, now)
        result = {"passes": passes,
                  "next_visible": next((p for p in passes if p["visible"]), None)}
        self._results[key] = (now, result)
        if len(self._results) > 200:
            self._results.clear()
        return result
