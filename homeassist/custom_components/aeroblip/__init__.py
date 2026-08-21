"""The Aeroblip integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AeroblipAuthError, AeroblipClient, AeroblipConnectionError
from .const import (
    CONF_AIRPORT,
    CONF_AREA_NM,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    CONF_RADIUS_NM,
    DOMAIN,
    SERVICE_UNWATCH_FLIGHT,
    SERVICE_WATCH_FLIGHT,
)
from .coordinator import AeroblipCoordinator
from .stats import AeroblipStats
from .watch import AeroblipWatchManager

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.GEO_LOCATION,
    Platform.IMAGE,
    Platform.SENSOR,
]

type AeroblipConfigEntry = ConfigEntry[AeroblipCoordinator]

_WATCH_SERVICE_SCHEMA = vol.Schema({vol.Required("callsign"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: AeroblipConfigEntry) -> bool:
    """Set up an Aeroblip config entry."""
    # Options win over the original setup data for the fields the options
    # flow can change (radius/area/airport); base_url/token/location are
    # immutable after setup and only ever live in entry.data.
    settings = {**entry.data, **entry.options}

    client = AeroblipClient(
        async_get_clientsession(hass),
        settings[CONF_BASE_URL],
        device_token=settings.get(CONF_DEVICE_TOKEN),
        latitude=settings[CONF_LATITUDE],
        longitude=settings[CONF_LONGITUDE],
        radius_nm=settings[CONF_RADIUS_NM],
        area_nm=settings[CONF_AREA_NM],
        airport=settings[CONF_AIRPORT],
    )

    try:
        await client.async_validate()
    except AeroblipAuthError as err:
        raise ConfigEntryAuthFailed("Aeroblip rejected the device token") from err
    except AeroblipConnectionError as err:
        raise ConfigEntryNotReady("Unable to connect to the Aeroblip server") from err

    coordinator = AeroblipCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    coordinator.async_start()

    # AeroblipCoordinator itself doesn't own stats (it's not this wave's to
    # edit) - attach it dynamically so sensor.py can reach it the same way
    # it reaches everything else: via the coordinator.
    stats = AeroblipStats(hass, entry, coordinator)
    await stats.async_setup()
    coordinator.stats = stats  # type: ignore[attr-defined]
    # async_on_unload accepts a callable whose *call* returns the awaitable
    # (Callable[[], Coroutine | None]) - passing the bound coroutine
    # function directly satisfies that, matching the idiom HA core itself
    # uses (e.g. yale/__init__.py's `entry.async_on_unload(data.async_stop)`,
    # ring/coordinator.py's `self.config_entry.async_on_unload(self.async_shutdown)`).
    entry.async_on_unload(stats.async_unload)

    # Same dynamic-attachment idiom as stats above - the coordinator itself
    # doesn't own the watch manager, but sensor.py and the services below
    # reach it through the coordinator.
    manager = AeroblipWatchManager(hass, entry, coordinator)
    await manager.async_setup()
    coordinator.watch_manager = manager  # type: ignore[attr-defined]
    entry.async_on_unload(manager.async_unload)

    _async_register_watch_services(hass)

    # Options (radius/area/airport) change the WebSocket query params, so a
    # new connection - not just new entity state - is needed on change.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_register_watch_services(hass: HomeAssistant) -> None:
    """Register watch_flight/unwatch_flight once at the domain level.

    A service call has no way to target one specific config entry, and
    running more than one Aeroblip server/location is a rare setup, so a
    watch is simply applied to (and removed from) every currently loaded
    entry - the sensible default for the common single-entry case, and
    harmless for the rare multi-entry one.
    """
    if hass.services.has_service(DOMAIN, SERVICE_WATCH_FLIGHT):
        return

    async def _async_watch_flight(call: ServiceCall) -> None:
        callsign = call.data["callsign"]
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                entry.runtime_data.watch_manager.async_add_watch(callsign)  # type: ignore[attr-defined]

    async def _async_unwatch_flight(call: ServiceCall) -> None:
        callsign = call.data["callsign"]
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                entry.runtime_data.watch_manager.async_remove_watch(callsign)  # type: ignore[attr-defined]

    hass.services.async_register(
        DOMAIN, SERVICE_WATCH_FLIGHT, _async_watch_flight, schema=_WATCH_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNWATCH_FLIGHT, _async_unwatch_flight, schema=_WATCH_SERVICE_SCHEMA
    )


async def async_unload_entry(hass: HomeAssistant, entry: AeroblipConfigEntry) -> bool:
    """Unload an Aeroblip config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Closing the socket lets the background WS task end promptly instead
        # of waiting for HA's own cancellation of the entry's background task.
        await entry.runtime_data.async_shutdown_client()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: AeroblipConfigEntry) -> None:
    """Reload the entry when options change so the WS reconnects with new params."""
    await hass.config_entries.async_reload(entry.entry_id)
