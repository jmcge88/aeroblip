"""Shared fixtures and canned WebSocket-frame snapshots for the Aeroblip tests.

Everything here runs fully offline: the AeroblipClient boundary
(async_validate / async_run / async_stop) is mocked so no real socket is
ever opened, and the module-level shim below makes sure the repo's
``custom_components.aeroblip`` package - not pytest-homeassistant-custom
-component's bundled stub package - is what gets imported.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pycares
import pytest
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

# aiodns/pycares lazily starts a process-wide "_run_safe_shutdown_loop"
# daemon thread the first time an aiohttp session with the default resolver
# is created (e.g. via async_get_clientsession in test_api.py). Start it now,
# at conftest import time - before PHACC's verify_cleanup fixture snapshots
# the thread set for the first test - or it gets flagged as a lingering
# thread and fails that test.
_PYCARES_WARMUP = pycares.Channel()

# --- make the repo's custom_components/ win the import race -----------------
_CUSTOM_COMPONENTS = Path(__file__).resolve().parent.parent / "custom_components"

for _name in [m for m in sys.modules if m.split(".")[0] == "custom_components"]:
    del sys.modules[_name]
_spec = importlib.util.spec_from_file_location(
    "custom_components",
    _CUSTOM_COMPONENTS / "__init__.py",
    submodule_search_locations=[str(_CUSTOM_COMPONENTS)],
)
_module = importlib.util.module_from_spec(_spec)
sys.modules["custom_components"] = _module
_spec.loader.exec_module(_module)

from custom_components.aeroblip.const import (  # noqa: E402
    CONF_AIRPORT,
    CONF_AREA_NM,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    CONF_RADIUS_NM,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


# --- canned data --------------------------------------------------------------

HEALTH = {"ok": True, "product": False, "meta_cache": {}}

OVERHEAD = {
    "aircraft": [
        {
            "hex": "abc123", "callsign": "QFA551", "registration": "VH-VZR", "type": "B738",
            "description": "BOEING 737-800", "lat": -27.40, "lon": 153.10, "altitude_ft": 6500,
            "ground_speed_kt": 285, "track": 15, "heading_cardinal": "NNE",
            "vertical_rate_fpm": -1400, "phase": "descending", "distance_nm": 2.1,
            "bearing_from_home": 45.0, "squawk": "3601", "emergency": None, "overhead": True,
            "route": {"origin": "SYD", "origin_name": "Sydney", "destination": "BNE",
                       "destination_name": "Brisbane", "airline": "Qantas", "airline_iata": "QF"},
            "airline": {"airline": "Qantas", "airline_iata": "QF"},
            "info": {"manufacturer": "Boeing", "model": "737-838", "owner": "Qantas",
                      "country": "Australia", "photo": None, "photo_thumb": None},
        },
        {
            "hex": "def456", "callsign": "JST812", "registration": None, "type": "A320",
            "description": None, "lat": -27.60, "lon": 153.30, "altitude_ft": 34000,
            "ground_speed_kt": 445, "track": 330, "heading_cardinal": "NNW",
            "vertical_rate_fpm": 0, "phase": "level", "distance_nm": 14.0,
            "bearing_from_home": 120.0, "squawk": None, "emergency": None, "overhead": False,
            "route": None, "airline": None, "info": None,
        },
    ],
    "updated": 1755763200, "provider": "adsblol", "overhead_count": 1,
    "overhead_radius_nm": 5.0, "area_radius_nm": 60.0,
}

ALERTS = {
    "aircraft": [
        {
            "hex": "eme001", "callsign": "EMR001", "registration": "VH-ABC", "type": "B738",
            "squawk": "7700", "place": "Tasman Sea", "distance_nm": 45.0,
            "route": {"origin": "BNE", "origin_name": "Brisbane", "destination": "AKL",
                       "destination_name": "Auckland", "airline": "Qantas", "airline_iata": "QF"},
        }
    ],
    "count": 1, "updated": 1755763200,
}

# Same overhead snapshot as OVERHEAD, but the nearest aircraft (aircraft[0],
# QFA551) carries a photo URL - for image.py tests, which key off
# info.photo. Kept as its own canned constant (rather than mutating OVERHEAD
# in place) so every test still gets an independent deepcopy.
OVERHEAD_WITH_PHOTO = copy.deepcopy(OVERHEAD)
OVERHEAD_WITH_PHOTO["aircraft"][0]["info"]["photo"] = (
    "https://cdn.planespotters.net/photo/000001-qfa551.jpg"
)


def make_board_snapshot(now: "dt_util.dt.datetime") -> dict:
    """Build a board snapshot relative to ``now`` so "next" logic is stable.

    One arrival 30 min out (should be picked), one cancelled arrival 10 min
    out (must be skipped despite being sooner), one arrival 2 h out (further
    away, must lose to the 30-min row), one departure 45 min out (should be
    picked), and one departure in the past beyond the 5-min grace window
    (must be skipped).
    """

    def iso(delta_minutes: float) -> str:
        return (now + timedelta(minutes=delta_minutes)).isoformat()

    arrivals = [
        {
            "flight": "QF551", "airline": "Qantas", "city": "Sydney", "code": "SYD",
            "scheduled": iso(30), "estimated": iso(30), "terminal": "D", "gate": "44",
            "status": "Scheduled", "aircraft": "B738", "direction": "arrival",
        },
        {
            "flight": "QF552", "airline": "Qantas", "city": "Melbourne", "code": "MEL",
            "scheduled": iso(10), "estimated": iso(10), "terminal": "D", "gate": "45",
            "status": "Cancelled", "aircraft": "B738", "direction": "arrival",
        },
        {
            "flight": "QF553", "airline": "Qantas", "city": "Perth", "code": "PER",
            "scheduled": iso(120), "estimated": iso(120), "terminal": "D", "gate": "46",
            "status": "Scheduled", "aircraft": "A330", "direction": "arrival",
        },
    ]
    departures = [
        {
            "flight": "QF700", "airline": "Qantas", "city": "Cairns", "code": "CNS",
            "scheduled": iso(45), "estimated": iso(45), "terminal": "D", "gate": "50",
            "status": "Scheduled", "aircraft": "B738", "direction": "departure",
        },
        {
            "flight": "QF701", "airline": "Qantas", "city": "Adelaide", "code": "ADL",
            "scheduled": iso(-10), "estimated": iso(-10), "terminal": "D", "gate": "51",
            "status": "Scheduled", "aircraft": "B738", "direction": "departure",
        },
    ]
    return {
        "arrivals": arrivals,
        "departures": departures,
        "updated": now.timestamp(),
        "airport": {"icao": "YBBN", "iata": "BNE", "name": "Brisbane"},
        "mock": False,
        "unavailable": False,
    }


# The 19 registry entities created for every config entry (geo_location
# entities, and per-callsign watch sensors, are dynamic/transient and are
# not part of this fixed set). Entity ids were confirmed against a live
# hass.states.async_entity_ids() dump rather than guessed - in particular,
# busiest_hour's translated name ("Busiest hour today") slugs to
# "busiest_hour_today", not the entity description key "busiest_hour".
KNOWN_ENTITY_IDS = [
    "sensor.aeroblip_bne_aircraft_overhead",
    "sensor.aeroblip_bne_aircraft_nearby",
    "sensor.aeroblip_bne_nearest_aircraft",
    "sensor.aeroblip_bne_nearest_bearing",
    "sensor.aeroblip_bne_next_flyover",
    "sensor.aeroblip_bne_next_arrival",
    "sensor.aeroblip_bne_next_departure",
    "sensor.aeroblip_bne_emergency_alerts",
    "sensor.aeroblip_bne_data_provider",
    "sensor.aeroblip_bne_flyovers_today",
    "sensor.aeroblip_bne_unique_aircraft_today",
    "sensor.aeroblip_bne_busiest_hour_today",
    "binary_sensor.aeroblip_bne_flight_overhead",
    "binary_sensor.aeroblip_bne_emergency_active",
    "binary_sensor.aeroblip_bne_server_connection",
    "event.aeroblip_bne_flyover",
    "event.aeroblip_bne_flyover_imminent",
    "event.aeroblip_bne_emergency",
    "image.aeroblip_bne_nearest_aircraft_photo",
]

@contextmanager
def mock_ws_client():
    """Patch AeroblipClient.async_run/async_stop so nothing touches a socket.

    Needed by any test that lets a config entry actually reach
    ``async_setup_entry`` (e.g. the config flow's ``async_create_entry``,
    which immediately sets the new entry up) - not just tests that patch
    ``async_validate`` for the flow's own pre-check.
    """

    async def fake_async_run(self, on_message, on_connection_change):
        await asyncio.Event().wait()

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_run",
        new=fake_async_run,
    ), patch(
        "custom_components.aeroblip.api.AeroblipClient.async_stop",
        new_callable=AsyncMock,
    ) as mock_stop:
        yield mock_stop


ENTRY_DATA = {
    CONF_BASE_URL: "http://192.168.1.10:8000",
    CONF_DEVICE_TOKEN: None,
    CONF_LATITUDE: -27.3842,
    CONF_LONGITUDE: 153.1175,
    CONF_RADIUS_NM: 5.0,
    CONF_AREA_NM: 60.0,
    CONF_AIRPORT: "BNE",
}


@pytest.fixture
async def setup_entry(hass):
    """Set up a fully-mocked Aeroblip config entry.

    Yields ``(entry, holder)`` where ``holder.on_message`` /
    ``holder.on_connection_change`` are the callbacks the coordinator handed
    to the (mocked) client's ``async_run`` - tests call them directly to
    inject frames, exactly as the real WebSocket loop would.
    """
    holder = SimpleNamespace(on_message=None, on_connection_change=None)

    async def fake_async_run(self, on_message, on_connection_change):
        holder.on_message = on_message
        holder.on_connection_change = on_connection_change
        # Blocks until the background task is cancelled at unload, mirroring
        # the lifetime of the real WS loop without opening any socket.
        await asyncio.Event().wait()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(ENTRY_DATA),
        title="Aeroblip (BNE)",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(return_value=HEALTH),
    ), patch(
        "custom_components.aeroblip.api.AeroblipClient.async_run",
        new=fake_async_run,
    ), patch(
        "custom_components.aeroblip.api.AeroblipClient.async_stop",
        new_callable=AsyncMock,
    ) as mock_stop:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        yield entry, holder, mock_stop

        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
