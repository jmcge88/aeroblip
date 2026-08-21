"""Tests for the aeroblip.watch_flight / aeroblip.unwatch_flight services and
the AeroblipWatchManager (custom_components/aeroblip/watch.py).

Frames are injected the same way as test_coordinator.py: via
holder.on_message/on_connection_change, followed by
hass.async_block_till_done().
"""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.aeroblip.const import STORAGE_KEY_WATCHES, STORAGE_VERSION

_WATCHED_CALLSIGN = "TST100"  # deliberately absent from conftest's OVERHEAD


def _frame(aircraft: list[dict], updated: float = 1755763200.0) -> dict:
    return {
        "aircraft": aircraft, "updated": updated, "provider": "adsblol",
        "overhead_count": sum(1 for a in aircraft if a.get("overhead")),
        "overhead_radius_nm": 5.0, "area_radius_nm": 60.0,
    }


def _watched_aircraft(*, overhead: bool) -> dict:
    return {
        "hex": "wat100", "callsign": _WATCHED_CALLSIGN, "registration": "VH-TST",
        "type": "PA28", "description": "PIPER PA-28", "lat": -27.40, "lon": 153.12,
        "altitude_ft": 4500, "ground_speed_kt": 110, "track": 200,
        "heading_cardinal": "SSW", "vertical_rate_fpm": -300, "phase": "descending",
        "distance_nm": 3.0 if overhead else 8.0, "bearing_from_home": 210.0,
        "squawk": "1200", "emergency": None, "overhead": overhead,
        "route": None, "airline": None, "info": None,
    }


async def test_watch_flight_creates_sensor_in_not_seen(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "aeroblip", "watch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.watch_tst100")
    assert state is not None
    assert state.state == "not_seen"


async def test_frame_with_watched_callsign_transitions_to_nearby(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    await hass.services.async_call(
        "aeroblip", "watch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()

    events: list[dict] = []
    hass.bus.async_listen("aeroblip_watched_flight", lambda event: events.append(event.data))

    # Baseline frame without the watched callsign - establishes _primed
    # without firing any transition (status stays "not_seen").
    holder.on_message("overhead", _frame([]))
    await hass.async_block_till_done()
    assert events == []
    assert hass.states.get("sensor.watch_tst100").state == "not_seen"

    holder.on_message("overhead", _frame([_watched_aircraft(overhead=False)]))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.watch_tst100").state == "nearby"
    assert len(events) == 1
    assert events[0]["callsign"] == _WATCHED_CALLSIGN
    assert events[0]["status"] == "nearby"
    assert events[0]["previous_status"] == "not_seen"


async def test_overhead_frame_transitions_to_overhead(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    await hass.services.async_call(
        "aeroblip", "watch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()

    events: list[dict] = []
    hass.bus.async_listen("aeroblip_watched_flight", lambda event: events.append(event.data))

    holder.on_message("overhead", _frame([]))  # baseline
    holder.on_message("overhead", _frame([_watched_aircraft(overhead=False)]))  # -> nearby
    await hass.async_block_till_done()

    holder.on_message("overhead", _frame([_watched_aircraft(overhead=True)]))  # -> overhead
    await hass.async_block_till_done()

    assert hass.states.get("sensor.watch_tst100").state == "overhead"
    overhead_events = [e for e in events if e["status"] == "overhead"]
    assert len(overhead_events) == 1
    assert overhead_events[0]["previous_status"] == "nearby"


async def test_frame_without_it_transitions_to_gone(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    await hass.services.async_call(
        "aeroblip", "watch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()

    events: list[dict] = []
    hass.bus.async_listen("aeroblip_watched_flight", lambda event: events.append(event.data))

    holder.on_message("overhead", _frame([]))  # baseline
    holder.on_message("overhead", _frame([_watched_aircraft(overhead=True)]))  # -> overhead
    await hass.async_block_till_done()

    holder.on_message("overhead", _frame([]))  # dropped off the frame -> gone
    await hass.async_block_till_done()

    assert hass.states.get("sensor.watch_tst100").state == "gone"
    gone_events = [e for e in events if e["status"] == "gone"]
    assert len(gone_events) == 1
    assert gone_events[0]["previous_status"] == "overhead"


async def test_unwatch_flight_removes_entity_and_registry_entry(hass, setup_entry):
    entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    await hass.services.async_call(
        "aeroblip", "watch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.watch_tst100") is not None

    registry = er.async_get(hass)
    assert registry.async_get("sensor.watch_tst100") is not None

    await hass.services.async_call(
        "aeroblip", "unwatch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.watch_tst100") is None
    assert registry.async_get("sensor.watch_tst100") is None
    assert _WATCHED_CALLSIGN not in entry.runtime_data.watch_manager.watches


async def test_watches_persist_via_store_save(hass, setup_entry):
    """Watches survive a restart via helpers.storage.Store, the same idiom
    AeroblipStats uses (see watch.py's _schedule_save/_as_stored_dict).

    async_add_watch only *schedules* a debounced write (async_delay_save,
    10 s later) rather than writing synchronously - reloading the config
    entry under the setup_entry fixture's patches turned out awkward (the
    fixture's own teardown unloads the entry, and a mid-test reload needs
    the same AeroblipClient patches re-applied to the new instance), so
    persistence is verified more directly here: force the manager's
    already-scheduled state through the same Store.async_save() call the
    debounce would eventually make, then load it back with a *second*,
    independent Store pointed at the identical (storage-version, key) pair
    to confirm the on-disk (in the test harness, in-memory via PHACC's
    hass_storage fixture) representation round-trips correctly.
    """
    entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    await hass.services.async_call(
        "aeroblip", "watch_flight", {"callsign": _WATCHED_CALLSIGN.lower()}, blocking=True
    )
    await hass.async_block_till_done()

    manager = entry.runtime_data.watch_manager
    await manager._store.async_save(manager._as_stored_dict())

    reloaded_store: Store = Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY_WATCHES}_{entry.entry_id}"
    )
    stored = await reloaded_store.async_load()

    assert stored is not None
    assert _WATCHED_CALLSIGN in stored["watches"]
