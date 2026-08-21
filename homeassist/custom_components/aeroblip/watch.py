"""Flight-watch manager for the Aeroblip integration.

Lets a service call track a specific ICAO callsign: a dynamic sensor
entity is created per watched callsign (see sensor.py's AeroblipWatchSensor),
and EVENT_WATCHED_FLIGHT fires on the bus whenever that callsign's tracking
status changes (not_seen -> nearby -> overhead -> gone). Watches persist
across restarts via homeassistant.helpers.storage.Store, following the same
idiom as AeroblipStats.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import EVENT_WATCHED_FLIGHT, STORAGE_KEY_WATCHES, STORAGE_VERSION
from .coordinator import AeroblipCoordinator

# Debounce persistence writes behind this many seconds - watches change
# rarely (a user calling a service), but _on_update runs on every ~5s WS
# frame, so any per-frame save must never be synchronous.
_SAVE_DELAY_S = 10


class AeroblipWatchManager:
    """Tracks user-requested flight watches and republishes their live status."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: AeroblipCoordinator
    ) -> None:
        """Initialize with fresh (pre-load) defaults; async_setup does the real load."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_WATCHES}_{entry.entry_id}"
        )
        self._watches: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, str] = {}
        self._aircraft: dict[str, dict[str, Any] | None] = {}
        # Callsigns matched at least once this session, so a later drop-out
        # of frame can be reported as "gone" rather than "not_seen".
        self._seen_this_session: set[str] = set()
        # A (re)connect must not replay transition events for watches whose
        # status was already established before the reconnect happened -
        # mirrors AeroblipCoordinator._overhead_primed.
        self._primed = False
        self._was_connected = False
        # Identity of the last overhead aircraft list handled by _on_update
        # (mirrors stats.py's _last_aircraft_frame): a board/alerts-only
        # push leaves this reference untouched (see
        # coordinator._handle_message), so there's nothing new to scan.
        self._last_aircraft_frame: list[dict[str, Any]] | None = None
        self._watch_listeners: dict[
            str, list[Callable[[str, dict[str, Any] | None], None]]
        ] = {}
        self._entities: dict[str, Entity] = {}
        self._add_entity_callback: Callable[[str], None] | None = None
        self._unsub_update: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Load persisted watches (if any) and subscribe to coordinator updates."""
        stored = await self._store.async_load()
        if stored is not None:
            self._watches = dict(stored.get("watches") or {})
        # Missing store -> the fresh empty defaults set in __init__ stand as-is.
        self._unsub_update = self._coordinator.async_add_listener(self._on_update)

    async def async_unload(self) -> None:
        """Unsubscribe from coordinator updates and flush state to disk."""
        if self._unsub_update is not None:
            self._unsub_update()
            self._unsub_update = None
        # Final, synchronous save - unlike the hot-path handler, this runs
        # once at unload time so there's no reason to debounce it.
        await self._store.async_save(self._as_stored_dict())

    def _as_stored_dict(self) -> dict[str, Any]:
        """Return the current in-memory watch list in its JSON-serialisable form."""
        return {"watches": self._watches}

    def _schedule_save(self) -> None:
        """Debounce a write; never save synchronously from a hot-path handler."""
        self._store.async_delay_save(self._as_stored_dict, _SAVE_DELAY_S)

    @callback
    def set_add_entity_callback(self, cb: Callable[[str], None]) -> None:
        """Register the sensor platform's hook for creating a watch entity."""
        self._add_entity_callback = cb

    @callback
    def register_entity(self, callsign: str, entity: Entity) -> None:
        """Record the live entity instance backing a watch, for later removal."""
        self._entities[callsign] = entity

    @callback
    def unregister_entity(self, callsign: str, entity: Entity) -> None:
        """Drop the entity reference recorded by register_entity.

        Guarded on identity so a stale entity's teardown can't clobber a
        newer entity registered for the same callsign (e.g. a rapid
        unwatch/watch of the same callsign).
        """
        if self._entities.get(callsign) is entity:
            del self._entities[callsign]

    @callback
    def async_add_watch(self, callsign: str) -> None:
        """Start tracking a callsign: persist it and create its sensor entity."""
        callsign = callsign.strip().upper()
        if callsign in self._watches:
            return
        self._watches[callsign] = {"added": dt_util.utcnow().isoformat()}
        self._schedule_save()
        if self._add_entity_callback is not None:
            self._add_entity_callback(callsign)

    @callback
    def async_remove_watch(self, callsign: str) -> None:
        """Stop tracking a callsign: persist the removal and drop its entity."""
        callsign = callsign.strip().upper()
        if callsign not in self._watches:
            return
        del self._watches[callsign]
        self._statuses.pop(callsign, None)
        self._aircraft.pop(callsign, None)
        self._seen_this_session.discard(callsign)
        self._watch_listeners.pop(callsign, None)
        self._schedule_save()

        entity = self._entities.get(callsign)
        if entity is not None and entity.hass is not None:
            self.hass.async_create_task(self._async_remove_entity(callsign, entity))

    async def _async_remove_entity(self, callsign: str, entity: Entity) -> None:
        """Remove a watch's entity from state and its entity registry entry.

        Removing just the entity leaves a disabled-looking orphan behind in
        the registry, so the registry entry is dropped too.
        """
        await entity.async_remove()
        registry = er.async_get(self.hass)
        if entity.entity_id in registry.entities:
            registry.async_remove(entity.entity_id)

    @callback
    def async_add_watch_listener(
        self, callsign: str, cb: Callable[[str, dict[str, Any] | None], None]
    ) -> CALLBACK_TYPE:
        """Register a callback invoked with (status, aircraft-or-None) on transition."""
        callsign = callsign.strip().upper()
        self._watch_listeners.setdefault(callsign, []).append(cb)

        def _remove() -> None:
            listeners = self._watch_listeners.get(callsign)
            if listeners and cb in listeners:
                listeners.remove(cb)

        return _remove

    def current(self, callsign: str) -> tuple[str, dict[str, Any] | None]:
        """Return a watch's current (status, aircraft-or-None) without waiting for a push."""
        callsign = callsign.strip().upper()
        return self._statuses.get(callsign, "not_seen"), self._aircraft.get(callsign)

    @property
    def watches(self) -> list[str]:
        """Return the currently-watched callsigns."""
        return list(self._watches)

    @callback
    def _on_update(self) -> None:
        """Recompute each watch's status from the latest overhead frame.

        Runs on every coordinator push (~5s); the per-aircraft scan below
        is skipped whenever the overhead aircraft list is the same object
        as last time (a board/alerts-only push - see
        coordinator._handle_message) and whenever there are no active
        watches, keeping the common case cheap.
        """
        data = self._coordinator.data
        connected = data.connected
        if connected and not self._was_connected:
            # Next scan is a fresh baseline, not new events - mirrors
            # AeroblipCoordinator._handle_connection.
            self._primed = False
        self._was_connected = connected

        overhead = data.overhead
        aircraft = overhead.get("aircraft") if overhead else None
        if aircraft is None or aircraft is self._last_aircraft_frame:
            return
        self._last_aircraft_frame = aircraft

        if not self._watches:
            self._primed = True
            return

        by_callsign: dict[str, dict[str, Any]] = {}
        for craft in aircraft:
            raw_callsign = craft.get("callsign")
            if raw_callsign:
                by_callsign.setdefault(raw_callsign.strip().upper(), craft)

        for callsign in list(self._watches):
            match = by_callsign.get(callsign)
            if match is not None:
                status = "overhead" if match.get("overhead") else "nearby"
                self._seen_this_session.add(callsign)
            elif callsign in self._seen_this_session:
                status = "gone"
            else:
                status = "not_seen"

            previous = self._statuses.get(callsign, "not_seen")
            self._statuses[callsign] = status
            self._aircraft[callsign] = match

            if self._primed and status != previous:
                self.hass.bus.async_fire(
                    EVENT_WATCHED_FLIGHT,
                    {
                        "callsign": callsign,
                        "status": status,
                        "previous_status": previous,
                        "aircraft": match,
                        "entry_id": self._entry.entry_id,
                    },
                )
                for listener in list(self._watch_listeners.get(callsign, [])):
                    listener(status, match)

        self._primed = True
