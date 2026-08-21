"""Diagnostics support for the Aeroblip integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant

from . import AeroblipConfigEntry
from .const import CONF_DEVICE_TOKEN

TO_REDACT = {CONF_DEVICE_TOKEN, CONF_LATITUDE, CONF_LONGITUDE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AeroblipConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    overhead = coordinator.data.overhead or {}
    board = coordinator.data.board or {}
    alerts = coordinator.data.alerts or {}

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "connected": coordinator.data.connected,
        "overhead": {
            "updated": overhead.get("updated"),
            "provider": overhead.get("provider"),
            "overhead_count": overhead.get("overhead_count"),
            "aircraft_count": len(overhead.get("aircraft") or []),
        },
        "board": {
            "updated": board.get("updated"),
            "airport": board.get("airport"),
            "mock": board.get("mock"),
            "unavailable": board.get("unavailable"),
            "arrivals": len(board.get("arrivals") or []),
            "departures": len(board.get("departures") or []),
        },
        "alerts": {
            "updated": alerts.get("updated"),
            "count": alerts.get("count"),
        },
    }
