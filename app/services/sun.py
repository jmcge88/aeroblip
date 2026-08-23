"""Solar position, for golden-hour tagging.

Photographers care whether a flyover happens in usable light. Rather than
buying a sunrise/sunset API, compute the sun's elevation locally: NOAA's
low-precision solar position algorithm is accurate to ~0.2 degrees, which is
far tighter than the concept of "golden hour" itself.
"""
from __future__ import annotations

import math
import time

# Golden light: sun low but contributing - from a little below the horizon
# (afterglow still lights the belly of an aircraft) up to ~8 degrees. Below
# NIGHT_ELEVATION_DEG there is nothing left to shoot by.
GOLDEN_LOW_DEG = -4.0
GOLDEN_HIGH_DEG = 8.0
NIGHT_ELEVATION_DEG = -6.0  # civil twilight ends; also used by the ISS watch


def sun_position(lat: float, lon: float, ts: float | None = None) -> tuple[float, float]:
    """(elevation_deg, azimuth_deg) of the sun at a place and unix time."""
    t = time.time() if ts is None else ts
    d = t / 86400.0 - 10957.5  # days since J2000.0 epoch
    g = math.radians((357.529 + 0.98560028 * d) % 360.0)   # mean anomaly
    q = (280.459 + 0.98564736 * d) % 360.0                 # mean longitude
    ecl = math.radians((q + 1.915 * math.sin(g)
                        + 0.020 * math.sin(2 * g)) % 360.0)  # ecliptic longitude
    e = math.radians(23.439 - 0.00000036 * d)              # obliquity
    ra = math.degrees(math.atan2(math.cos(e) * math.sin(ecl), math.cos(ecl))) % 360.0
    dec = math.asin(math.sin(e) * math.sin(ecl))
    gmst_h = (18.697374558 + 24.06570982441908 * d) % 24.0
    ha = math.radians(((gmst_h * 15.0 + lon - ra) + 540.0) % 360.0 - 180.0)
    lat_r = math.radians(lat)
    el = math.asin(math.sin(lat_r) * math.sin(dec)
                   + math.cos(lat_r) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha),
                    math.tan(dec) * math.cos(lat_r) - math.sin(lat_r) * math.cos(ha))
    return math.degrees(el), math.degrees(az) % 360.0


def sun_eci_unit(ts: float) -> tuple[float, float, float]:
    """Unit vector to the sun in earth-centred inertial coordinates - used by
    the ISS watch to decide whether the station is sunlit."""
    d = ts / 86400.0 - 10957.5
    g = math.radians((357.529 + 0.98560028 * d) % 360.0)
    q = (280.459 + 0.98564736 * d) % 360.0
    ecl = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360.0)
    e = math.radians(23.439 - 0.00000036 * d)
    return (math.cos(ecl),
            math.cos(e) * math.sin(ecl),
            math.sin(e) * math.sin(ecl))


def light_info(lat: float, lon: float, ts: float | None = None) -> dict:
    """Snapshot-embeddable light summary for one location."""
    el, az = sun_position(lat, lon, ts)
    if el > GOLDEN_HIGH_DEG:
        light = "day"
    elif el >= GOLDEN_LOW_DEG:
        light = "golden"
    elif el >= NIGHT_ELEVATION_DEG:
        light = "twilight"
    else:
        light = "night"
    return {"elevation_deg": round(el, 1), "azimuth_deg": round(az),
            "light": light, "golden": light == "golden"}
