"""Flyover prediction geometry.

Pure math, no Home Assistant imports, so it can be unit-tested in
isolation from the coordinator that calls it.
"""
from __future__ import annotations

import math
from typing import Any

# Below this ground speed an aircraft's reported track is noise (taxiing,
# holding, wind-blown drift) and projecting it forward produces nonsense.
_STATIONARY_KT: float = 40.0


def seconds_until_overhead(
    aircraft: dict[str, Any],
    home_lat: float,
    home_lon: float,
    radius_nm: float,
    horizon_s: float,
) -> float | None:
    """Return seconds until ``aircraft``'s straight-line path enters the overhead ring.

    Position is converted to a flat-earth NM offset from home - fine at the
    tens-of-NM ranges this integration cares about:
        x = (lon - home_lon) * 60 * cos(radians(home_lat))   # east-positive
        y = (lat - home_lat) * 60                             # north-positive
    Velocity (NM/s) is decomposed from ground speed and true track (0 deg = north):
        vx = v * sin(radians(track)), vy = v * cos(radians(track))

    The aircraft is at p + v*t; it enters the ring of radius ``radius_nm``
    at the smallest t solving |p + v*t| = radius_nm, i.e. the quadratic
    (vx^2+vy^2) t^2 + 2(x*vx+y*vy) t + (x^2+y^2-r^2) = 0. Returns None if
    there is no real, positive, in-horizon root, or if the aircraft is
    already inside/overhead, missing the fields needed to project it, or
    too slow for its track to be meaningful.
    """
    if aircraft.get("overhead"):
        return None
    lat = aircraft.get("lat")
    lon = aircraft.get("lon")
    track = aircraft.get("track")
    ground_speed_kt = aircraft.get("ground_speed_kt")
    if lat is None or lon is None or track is None or ground_speed_kt is None:
        return None
    if ground_speed_kt < _STATIONARY_KT:
        return None

    distance_nm = aircraft.get("distance_nm")
    if distance_nm is not None and distance_nm <= radius_nm:
        return None

    x = (lon - home_lon) * 60.0 * math.cos(math.radians(home_lat))
    y = (lat - home_lat) * 60.0
    if distance_nm is None and math.hypot(x, y) <= radius_nm:
        return None

    v = ground_speed_kt / 3600.0
    vx = v * math.sin(math.radians(track))
    vy = v * math.cos(math.radians(track))

    a = vx * vx + vy * vy
    if a <= 0.0:
        return None  # effectively stationary once converted to NM/s

    b = 2.0 * (x * vx + y * vy)
    c = x * x + y * y - radius_nm * radius_nm

    discriminant = b * b - 4.0 * a * c
    if discriminant < 0.0:
        return None  # path never reaches the ring

    sqrt_disc = math.sqrt(discriminant)
    t1 = (-b - sqrt_disc) / (2.0 * a)
    t2 = (-b + sqrt_disc) / (2.0 * a)

    candidates = [t for t in (t1, t2) if t > 0.0]
    if not candidates:
        return None  # both roots are in the past

    eta_s = min(candidates)
    if eta_s > horizon_s:
        return None
    return eta_s
