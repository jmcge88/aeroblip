"""Tests for the Aeroblip push-mode coordinator, driven via injected frames.

Each test uses the ``setup_entry`` fixture to get a fully set-up config
entry with the AeroblipClient boundary mocked, then calls
``holder.on_connection_change`` / ``holder.on_message`` directly - exactly
the callbacks the coordinator handed to the (mocked) client's async_run -
to simulate WebSocket traffic, followed by ``hass.async_block_till_done()``.
"""
from __future__ import annotations

import copy

import pytest

import homeassistant.util.dt as dt_util

from conftest import ALERTS, ENTRY_DATA, OVERHEAD, make_board_snapshot
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE

# Home position used by the setup_entry fixture's config entry (see
# conftest.ENTRY_DATA) - reused here to build synthetic approach geometry
# for the flyover-prediction tests below.
_HOME_LAT = ENTRY_DATA[CONF_LATITUDE]
_HOME_LON = ENTRY_DATA[CONF_LONGITUDE]


def _approaching_frame(distance_nm: float, updated: float) -> dict:
    """Build an overhead frame with one aircraft ``distance_nm`` NM due
    north of home, tracking due south at 300 kt (home: -27.3842, 153.1175,
    overhead radius 5 NM per ENTRY_DATA) - close enough to due-north-closing
    geometry that its projected ETA is exactly (distance_nm - 5) / 300 * 3600
    seconds, with no flat-earth longitude skew to account for.
    """
    aircraft = {
        "hex": "appr01", "callsign": "APR001", "registration": "VH-APR", "type": "C172",
        "description": "TEST APPROACH", "lat": _HOME_LAT + distance_nm / 60.0,
        "lon": _HOME_LON, "altitude_ft": 3000, "ground_speed_kt": 300, "track": 180,
        "heading_cardinal": "S", "vertical_rate_fpm": 0, "phase": "level",
        "distance_nm": distance_nm, "bearing_from_home": 0.0, "squawk": "1200",
        "emergency": None, "overhead": False, "route": None, "airline": None, "info": None,
    }
    return {
        "aircraft": [aircraft], "updated": updated, "provider": "adsblol",
        "overhead_count": 0, "overhead_radius_nm": 5.0, "area_radius_nm": 60.0,
    }


async def test_overhead_frame_populates_sensors(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_aircraft_nearby").state == "2"
    assert hass.states.get("sensor.aeroblip_bne_aircraft_overhead").state == "1"

    nearest = hass.states.get("sensor.aeroblip_bne_nearest_aircraft")
    assert nearest.state == "QFA551"
    assert nearest.attributes["origin"] == "SYD"
    assert nearest.attributes["destination"] == "BNE"
    assert nearest.attributes["airline"] == "Qantas"

    assert hass.states.get("binary_sensor.aeroblip_bne_flight_overhead").state == "on"


async def test_connection_lost_marks_data_sensors_unavailable(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    holder.on_connection_change(False)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_nearest_aircraft").state == "unavailable"
    # The connection sensor itself must stay available and report "off",
    # not unavailable - it exists specifically to show the link is down.
    assert hass.states.get("binary_sensor.aeroblip_bne_server_connection").state == "off"


async def test_flyover_baseline_then_new_aircraft_fires_event(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    events: list[dict] = []
    hass.bus.async_listen("aeroblip_flyover", lambda event: events.append(event.data))

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)  # QFA551 already overhead: baseline only
    await hass.async_block_till_done()

    assert events == [], "baseline frame after connect must not fire flyover"

    overhead2 = copy.deepcopy(OVERHEAD)
    overhead2["aircraft"][1]["overhead"] = True  # JST812 newly overhead
    overhead2["aircraft"][1]["distance_nm"] = 3.0
    overhead2["overhead_count"] = 2
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["aircraft"]["callsign"] == "JST812"

    ev_state = hass.states.get("event.aeroblip_bne_flyover")
    assert ev_state.state not in ("unknown", "unavailable")
    assert ev_state.attributes["callsign"] == "JST812"


async def test_reconnect_establishes_new_baseline(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    events: list[dict] = []
    hass.bus.async_listen("aeroblip_flyover", lambda event: events.append(event.data))

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)  # QFA551 overhead: baseline
    await hass.async_block_till_done()

    holder.on_connection_change(False)
    await hass.async_block_till_done()
    holder.on_connection_change(True)  # resets the primed flag
    await hass.async_block_till_done()

    overhead2 = copy.deepcopy(OVERHEAD)
    overhead2["aircraft"][1]["overhead"] = True  # JST812 newly overhead post-reconnect
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    assert events == [], "first frame after a reconnect is a new baseline, not events"


async def test_alerts_frame_and_new_emergency_fires_event(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    events: list[dict] = []
    hass.bus.async_listen("aeroblip_emergency", lambda event: events.append(event.data))

    holder.on_connection_change(True)
    holder.on_message("alerts", ALERTS)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_emergency_alerts").state == "1"
    assert hass.states.get("binary_sensor.aeroblip_bne_emergency_active").state == "on"
    assert events == [], "baseline alerts frame must not fire emergency"

    alerts2 = copy.deepcopy(ALERTS)
    alerts2["aircraft"].append(
        {
            "hex": "eme002", "callsign": "XYZ999", "registration": "VH-XYZ", "type": "A320",
            "squawk": "7700", "place": "Coral Sea", "distance_nm": 60.0,
            "route": {"origin": "BNE", "origin_name": "Brisbane", "destination": "SYD",
                       "destination_name": "Sydney", "airline": "Jetstar", "airline_iata": "JQ"},
        }
    )
    alerts2["count"] = 2
    holder.on_message("alerts", alerts2)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["aircraft"]["hex"] == "eme002"

    ev_state = hass.states.get("event.aeroblip_bne_emergency")
    assert ev_state.state not in ("unknown", "unavailable")


async def test_board_frames_pick_next_arrival_and_departure(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    board = make_board_snapshot(dt_util.now())

    holder.on_connection_change(True)
    holder.on_message("board", board)
    await hass.async_block_till_done()

    arrival = hass.states.get("sensor.aeroblip_bne_next_arrival")
    assert arrival.state == "QF551"  # 30-min row, not the cancelled 10-min row

    departure = hass.states.get("sensor.aeroblip_bne_next_departure")
    assert departure.state == "QF700"  # 45-min row, not the past one beyond grace


async def test_emergency_geo_location_marker(hass, setup_entry):
    """A 7700 alert with a position gets its own marker in the emergency source."""
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    alerts = copy.deepcopy(ALERTS)
    alerts["aircraft"][0].update(
        {"lat": -29.2, "lon": 159.8, "track": 231.0, "altitude_ft": 21000,
         "ground_speed_kt": 470, "heading_cardinal": "SW", "emergency": "general"}
    )
    holder.on_message("alerts", alerts)
    await hass.async_block_till_done()

    state = hass.states.get("geo_location.emr001")
    assert state is not None, "no emergency map marker was created"
    assert state.attributes["source"] == "aeroblip_emergency"
    assert state.attributes["place"] == "Tasman Sea"
    assert state.attributes["entity_picture"].startswith("data:image/svg+xml,")

    # An alert with no position must not create a marker (and not crash the sync)
    alerts2 = copy.deepcopy(alerts)
    alerts2["aircraft"].append({"hex": "nopos1", "callsign": "XYZ111", "squawk": "7700"})
    alerts2["count"] = 2
    holder.on_message("alerts", alerts2)
    await hass.async_block_till_done()
    assert hass.states.get("geo_location.xyz111") is None
    assert hass.states.get("geo_location.emr001") is not None

    # Cleared alerts remove the marker
    holder.on_message("alerts", {"aircraft": [], "count": 0, "updated": 1755763300})
    await hass.async_block_till_done()
    assert hass.states.get("geo_location.emr001") is None


async def test_geo_location_lifecycle(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    qfa = hass.states.get("geo_location.qfa551")
    jst = hass.states.get("geo_location.jst812")
    assert qfa is not None
    assert jst is not None
    assert qfa.state == "3.9"  # 2.1 nm * 1.852 km/nm, rounded to 1dp

    overhead_no_jst = copy.deepcopy(OVERHEAD)
    overhead_no_jst["aircraft"] = [overhead_no_jst["aircraft"][0]]
    holder.on_message("overhead", overhead_no_jst)
    await hass.async_block_till_done()

    assert hass.states.get("geo_location.jst812") is None
    assert hass.states.get("geo_location.qfa551") is not None

    holder.on_connection_change(False)
    await hass.async_block_till_done()

    assert hass.states.get("geo_location.qfa551") is None


async def test_imminent_flyover_fires_once_then_suppressed(hass, setup_entry):
    """An approaching aircraft fires aeroblip_flyover_imminent exactly once
    as it crosses the 90 s ETA threshold, not on every subsequent frame."""
    _entry, holder, _mock_stop = setup_entry

    events: list[dict] = []
    hass.bus.async_listen(
        "aeroblip_flyover_imminent", lambda event: events.append(event.data)
    )

    holder.on_connection_change(True)
    # Baseline frame: 60 NM out: eta = 55/300*3600 = 660 s, nowhere near the
    # 90 s imminent threshold, and the very first frame after (re)connect
    # never fires imminent events anyway (see _prediction_primed).
    holder.on_message("overhead", _approaching_frame(60.0, updated=1755763200))
    await hass.async_block_till_done()
    assert events == [], "baseline frame after connect must not fire imminent"

    # Second frame: 12 NM out: eta = 7/300*3600 = 84 s, inside the threshold.
    holder.on_message("overhead", _approaching_frame(12.0, updated=1755763205))
    await hass.async_block_till_done()

    assert len(events) == 1
    fired = events[0]
    assert fired["aircraft"]["callsign"] == "APR001"
    assert fired["eta_s"] == pytest.approx(84.0, abs=1.0)

    ev_state = hass.states.get("event.aeroblip_bne_flyover_imminent")
    assert ev_state.state not in ("unknown", "unavailable")
    assert ev_state.attributes["callsign"] == "APR001"
    assert ev_state.attributes["eta_seconds"] == pytest.approx(84, abs=1)

    next_flyover = hass.states.get("sensor.aeroblip_bne_next_flyover")
    assert next_flyover.state not in ("unknown", "unavailable")
    assert next_flyover.attributes["eta_seconds"] == pytest.approx(84, abs=1)

    # Third, identical frame: same hex, same close ETA - must not re-fire.
    holder.on_message("overhead", _approaching_frame(12.0, updated=1755763210))
    await hass.async_block_till_done()

    assert len(events) == 1, "an unchanged approach must not re-fire the imminent event"


async def test_nearest_bearing_sensor_state_and_cardinal(hass, setup_entry):
    """The bearing sensor surfaces the nearest aircraft's bearing_from_home
    as its state, with the matching 16-point cardinal as an attribute."""
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    # aircraft[0] (QFA551, the nearest) carries bearing_from_home 45.0,
    # which is exactly on the NE sector's centre point (round(45/22.5)==2).
    bearing = hass.states.get("sensor.aeroblip_bne_nearest_bearing")
    assert float(bearing.state) == pytest.approx(45.0)
    assert bearing.attributes["cardinal"] == "NE"
    assert bearing.attributes["callsign"] == "QFA551"
