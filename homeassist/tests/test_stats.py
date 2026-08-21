"""Tests for AeroblipStats (tests/../custom_components/aeroblip/stats.py).

Driven the same way as test_coordinator.py: the setup_entry fixture yields
(entry, holder, mock_stop), and frames are injected via
holder.on_message/on_connection_change followed by
hass.async_block_till_done().
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util

from conftest import OVERHEAD


async def test_flyover_increments_flyovers_today_sensor(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)  # QFA551 already overhead: baseline only
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_flyovers_today").state == "0"

    overhead2 = copy.deepcopy(OVERHEAD)
    overhead2["aircraft"][1]["overhead"] = True  # JST812 newly overhead
    overhead2["overhead_count"] = 2
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_flyovers_today").state == "1"


async def test_unique_aircraft_counts_distinct_hexes_across_frames(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)  # hexes abc123, def456
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").state == "2"

    overhead2 = copy.deepcopy(OVERHEAD)
    # Same two hexes again, plus one brand-new one - only the new hex
    # should push the count up.
    overhead2["aircraft"].append(
        {
            "hex": "ghi789", "callsign": "VOZ100", "registration": "VH-VOZ", "type": "B738",
            "description": "BOEING 737-800", "lat": -27.30, "lon": 153.05, "altitude_ft": 12000,
            "ground_speed_kt": 320, "track": 200, "heading_cardinal": "SSW",
            "vertical_rate_fpm": -800, "phase": "descending", "distance_nm": 22.0,
            "bearing_from_home": 300.0, "squawk": "3602", "emergency": None, "overhead": False,
            "route": None, "airline": None, "info": None,
        }
    )
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").state == "3"

    # Resending the very same three-hex frame must not double count.
    holder.on_message("overhead", copy.deepcopy(overhead2))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").state == "3"


async def test_busiest_hour_formats_hh_00(hass, setup_entry, freezer):
    """Freeze a moment in time and check the sensor formats *that* local
    hour as "HH:00" - the assertion is derived from dt_util.now() rather
    than a hardcoded wall-clock hour, so it holds regardless of whatever
    default time zone the test hass fixture is configured with."""
    _entry, holder, _mock_stop = setup_entry

    freezer.move_to("2026-08-21T12:00:00+00:00")
    frozen_hour = dt_util.now().hour

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)  # baseline
    await hass.async_block_till_done()

    overhead2 = copy.deepcopy(OVERHEAD)
    overhead2["aircraft"][1]["overhead"] = True  # fires one flyover at frozen_hour
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    busiest = hass.states.get("sensor.aeroblip_bne_busiest_hour_today")
    assert busiest.state == f"{frozen_hour:02d}:00"
    assert busiest.attributes["hour_counts"] == {str(frozen_hour): 1}


async def test_rare_aircraft_baseline_silent_then_new_type_fires_once(hass, setup_entry):
    events: list[dict] = []
    _entry, holder, _mock_stop = setup_entry
    hass.bus.async_listen("aeroblip_rare_aircraft", lambda event: events.append(event.data))

    holder.on_connection_change(True)
    # OVERHEAD's two aircraft are typed B738 and A320 - the first frame with
    # any types at all is the cold-start baseline for the all-time registry,
    # so it must stay silent even though both types are "new".
    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    assert events == [], "the first (cold-start) frame with types must not fire events"

    overhead2 = copy.deepcopy(OVERHEAD)
    overhead2["aircraft"].append(
        {
            "hex": "rare001", "callsign": "RAR001", "registration": "VH-RAR", "type": "A388",
            "description": "AIRBUS A380-800", "lat": -27.35, "lon": 153.12, "altitude_ft": 15000,
            "ground_speed_kt": 260, "track": 90, "heading_cardinal": "E",
            "vertical_rate_fpm": -500, "phase": "descending", "distance_nm": 8.0,
            "bearing_from_home": 90.0, "squawk": "3603", "emergency": None, "overhead": False,
            "route": None, "airline": None, "info": None,
        }
    )
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["aircraft"]["type"] == "A388"
    assert events[0]["first_seen"] is True

    # Resending the same (now-known) type must not fire a second event.
    holder.on_message("overhead", copy.deepcopy(overhead2))
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_stats_sensors_remain_available_when_connection_drops(hass, setup_entry):
    """Contrast with AeroblipEntity-gated sensors like nearest_aircraft,
    which go unavailable the moment the WebSocket connection drops - stats
    sensors override .available to bypass that gate (see
    AeroblipFlyoversTodaySensor.available and friends)."""
    _entry, holder, _mock_stop = setup_entry

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    holder.on_connection_change(False)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_nearest_aircraft").state == "unavailable"

    for entity_id in (
        "sensor.aeroblip_bne_flyovers_today",
        "sensor.aeroblip_bne_unique_aircraft_today",
        "sensor.aeroblip_bne_busiest_hour_today",
    ):
        state = hass.states.get(entity_id)
        assert state.state != "unavailable", f"{entity_id} went unavailable on disconnect"


async def test_midnight_rollover_resets_daily_counters_keeps_types_seen(
    hass, setup_entry, freezer
):
    """Local midnight rollover clears today's flyovers/unique-aircraft
    counters but leaves the all-time types_seen registry untouched."""
    _entry, holder, _mock_stop = setup_entry

    # Build "23:59:50 local" / "00:00:05 local the next day" from the test
    # hass fixture's own configured time zone (dt_util.DEFAULT_TIME_ZONE,
    # set during hass startup) rather than assuming a fixed UTC offset.
    tz = dt_util.DEFAULT_TIME_ZONE
    before_midnight = datetime(2026, 8, 21, 23, 59, 50, tzinfo=tz)
    after_midnight = before_midnight + timedelta(seconds=15)
    freezer.move_to(before_midnight)

    holder.on_connection_change(True)
    holder.on_message("overhead", OVERHEAD)  # baseline: seeds types_seen (B738, A320)
    await hass.async_block_till_done()

    overhead2 = copy.deepcopy(OVERHEAD)
    overhead2["aircraft"][1]["overhead"] = True  # one flyover before midnight
    holder.on_message("overhead", overhead2)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_flyovers_today").state == "1"
    assert hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").state == "2"
    types_before = hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").attributes[
        "types_seen_all_time"
    ]
    assert types_before == 2

    freezer.move_to(after_midnight)

    # Any coordinator push runs stats._on_update, which checks for rollover
    # before doing anything else - a fresh overhead frame (same aircraft,
    # nothing newly overhead) is enough to trigger it.
    holder.on_message("overhead", copy.deepcopy(overhead2))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.aeroblip_bne_flyovers_today").state == "0"
    assert hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").state == "2", (
        "the post-rollover frame still re-seeds today's hexes from scratch"
    )
    types_after = hass.states.get("sensor.aeroblip_bne_unique_aircraft_today").attributes[
        "types_seen_all_time"
    ]
    assert types_after == types_before, "types_seen is all-time and must survive rollover"
