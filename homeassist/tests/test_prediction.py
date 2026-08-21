"""Unit tests for the pure flyover-projection geometry in prediction.py.

No Home Assistant fixtures involved - seconds_until_overhead() takes plain
dicts/floats and returns a float or None, so it's exercised directly.
"""
from __future__ import annotations

import math

import pytest

from custom_components.aeroblip.prediction import seconds_until_overhead

HOME_LAT = -27.3842
HOME_LON = 153.1175
RADIUS_NM = 5.0
HORIZON_S = 900.0


def _aircraft(**overrides):
    base = {
        "hex": "test01",
        "lat": HOME_LAT,
        "lon": HOME_LON,
        "track": 180.0,
        "ground_speed_kt": 300.0,
        "overhead": False,
        "distance_nm": None,
    }
    base.update(overrides)
    return base


def test_due_north_closing_case():
    """20 NM due north, tracking due south at 300 kt, radius 5 -> ~180 s."""
    aircraft = _aircraft(lat=HOME_LAT + 20 / 60.0, lon=HOME_LON, distance_nm=20.0)

    eta = seconds_until_overhead(aircraft, HOME_LAT, HOME_LON, RADIUS_NM, HORIZON_S)

    assert eta is not None
    assert eta == pytest.approx(180.0, abs=0.5)


def test_abeam_track_never_enters_ring_returns_none():
    """20 NM abeam (east), flying due south so the perpendicular offset never
    closes below the radius - the straight-line path misses the ring entirely."""
    lon = HOME_LON + 20 / (60.0 * math.cos(math.radians(HOME_LAT)))
    aircraft = _aircraft(lat=HOME_LAT, lon=lon)

    eta = seconds_until_overhead(aircraft, HOME_LAT, HOME_LON, RADIUS_NM, HORIZON_S)

    assert eta is None


def test_already_overhead_returns_none():
    aircraft = _aircraft(overhead=True)

    eta = seconds_until_overhead(aircraft, HOME_LAT, HOME_LON, RADIUS_NM, HORIZON_S)

    assert eta is None


@pytest.mark.parametrize("missing_field", ["lat", "lon", "track", "ground_speed_kt"])
def test_missing_required_field_returns_none(missing_field):
    aircraft = _aircraft(lat=HOME_LAT + 20 / 60.0)
    aircraft[missing_field] = None

    eta = seconds_until_overhead(aircraft, HOME_LAT, HOME_LON, RADIUS_NM, HORIZON_S)

    assert eta is None


def test_slow_ground_speed_returns_none():
    """Below the stationary threshold (40 kt) the reported track is noise."""
    aircraft = _aircraft(lat=HOME_LAT + 20 / 60.0, ground_speed_kt=30.0)

    eta = seconds_until_overhead(aircraft, HOME_LAT, HOME_LON, RADIUS_NM, HORIZON_S)

    assert eta is None


def test_eta_beyond_horizon_returns_none():
    """Same geometry as the due-north closing case (~180 s) but with a
    horizon shorter than the projected ETA."""
    aircraft = _aircraft(lat=HOME_LAT + 20 / 60.0, lon=HOME_LON, distance_nm=20.0)

    eta = seconds_until_overhead(aircraft, HOME_LAT, HOME_LON, RADIUS_NM, horizon_s=60.0)

    assert eta is None


def test_tangent_edge_case_grazes_ring():
    """A straight-line path offset just inside the ring radius (4.999 NM vs.
    a 5 NM radius) produces a discriminant barely above zero - the smallest
    real, positive root the quadratic can produce - rather than the
    exactly-zero discriminant of a true geometric tangent, which is not
    reproducible in floating point (sin/cos of 180 deg aren't exactly 0/-1).
    """
    home_lat, home_lon = 0.0, 0.0
    radius_nm = 5.0
    x_nm, y_nm = 4.999, 20.0
    aircraft = _aircraft(lat=y_nm / 60.0, lon=x_nm / 60.0, distance_nm=None)

    eta = seconds_until_overhead(aircraft, home_lat, home_lon, radius_nm, HORIZON_S)

    assert eta is not None
    assert eta == pytest.approx(238.8, abs=1.0)

    # Nudge just outside the radius: the same path now misses entirely.
    aircraft_miss = _aircraft(lat=y_nm / 60.0, lon=5.001 / 60.0, distance_nm=None)
    assert (
        seconds_until_overhead(aircraft_miss, home_lat, home_lon, radius_nm, HORIZON_S)
        is None
    )
