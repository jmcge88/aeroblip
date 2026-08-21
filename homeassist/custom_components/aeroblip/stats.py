"""Daily spotting statistics for the Aeroblip integration.

Tracks flyover counts, unique airframes, and busiest hour per local day,
plus an all-time first-seen registry of aircraft types used to fire the
EVENT_RARE_AIRCRAFT bus event. State survives HA restarts via
homeassistant.helpers.storage.Store.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import EVENT_RARE_AIRCRAFT, STORAGE_KEY_STATS, STORAGE_VERSION
from .coordinator import AeroblipCoordinator

# Debounce writes behind this many seconds - _on_update runs on every ~5s
# WS frame and must never save synchronously in that hot path.
_SAVE_DELAY_S = 30


class AeroblipStats:
    """Accumulates daily spotting statistics from coordinator events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: AeroblipCoordinator,
    ) -> None:
        """Initialize with fresh (pre-load) defaults; async_setup does the real load."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_STATS}_{entry.entry_id}"
        )
        self._day: str = dt_util.now().date().isoformat()
        self._flyovers: int = 0
        self._hour_counts: dict[str, int] = {}
        self._hexes_today: set[str] = set()
        self._types_seen: dict[str, str] = {}
        # Identity of the last overhead aircraft list handled by
        # _on_update, so board/alerts-only pushes (which leave this field's
        # reference untouched - see coordinator._handle_message) skip the
        # per-aircraft scan entirely.
        self._last_aircraft_frame: list[dict[str, Any]] | None = None
        self._unsub_flyover: CALLBACK_TYPE | None = None
        self._unsub_update: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Load persisted state (if any) and subscribe to coordinator events."""
        stored = await self._store.async_load()
        if stored is not None:
            self._day = stored["day"]
            self._flyovers = stored["flyovers"]
            self._hour_counts = stored["hour_counts"]
            self._hexes_today = set(stored["hexes_today"])
            self._types_seen = stored["types_seen"]
        # Missing store -> the fresh defaults set in __init__ stand as-is.
        self._unsub_flyover = self._coordinator.async_add_flyover_listener(
            self._on_flyover
        )
        self._unsub_update = self._coordinator.async_add_listener(self._on_update)

    async def async_unload(self) -> None:
        """Unsubscribe from coordinator events and flush state to disk."""
        if self._unsub_flyover is not None:
            self._unsub_flyover()
            self._unsub_flyover = None
        if self._unsub_update is not None:
            self._unsub_update()
            self._unsub_update = None
        # Final, synchronous save - unlike the hot-path handlers, this runs
        # once at unload time so there's no reason to debounce it.
        await self._store.async_save(self._as_stored_dict())

    def _as_stored_dict(self) -> dict[str, Any]:
        """Return the current in-memory state in its JSON-serialisable form."""
        return {
            "day": self._day,
            "flyovers": self._flyovers,
            "hour_counts": self._hour_counts,
            "hexes_today": sorted(self._hexes_today),
            "types_seen": self._types_seen,
        }

    def _schedule_save(self) -> None:
        """Debounce a write; never save synchronously from a hot-path handler."""
        self._store.async_delay_save(self._as_stored_dict, _SAVE_DELAY_S)

    def _check_rollover(self) -> bool:
        """Reset the daily counters when local midnight has passed.

        types_seen is deliberately NOT reset here - it's the all-time
        first-seen registry, not a daily one. Returns True if a rollover
        happened, so callers know the reset itself needs persisting even
        when nothing else changed on this call.
        """
        today = dt_util.now().date().isoformat()
        if today == self._day:
            return False
        self._day = today
        self._flyovers = 0
        self._hour_counts = {}
        self._hexes_today = set()
        return True

    @callback
    def _on_flyover(self, aircraft: dict[str, Any]) -> None:
        """Record a flyover event against today's counters."""
        self._check_rollover()
        self._flyovers += 1
        hour_key = str(dt_util.now().hour)
        self._hour_counts[hour_key] = self._hour_counts.get(hour_key, 0) + 1
        self._schedule_save()

    @callback
    def _on_update(self) -> None:
        """Track unique airframes today and all-time first-seen aircraft types.

        Runs on every coordinator push (~5s). The rollover check is cheap
        (one date comparison) and always runs, but the per-aircraft scan
        below only runs when the overhead aircraft list is a genuinely new
        object - a board/alerts-only push leaves that reference untouched
        (see coordinator._handle_message), so there's nothing new to scan.
        """
        rolled_over = self._check_rollover()
        overhead = self._coordinator.data.overhead
        aircraft_frame = overhead.get("aircraft") if overhead else None

        if aircraft_frame is None or aircraft_frame is self._last_aircraft_frame:
            if rolled_over:
                self._schedule_save()
            return
        self._last_aircraft_frame = aircraft_frame

        # An empty types_seen at handler entry means nothing has ever been
        # recorded - this frame is the cold-start baseline, so everything
        # in it is only "new" because the registry itself is new, not
        # because it's rare. Firing EVENT_RARE_AIRCRAFT for all of them
        # would be spam, so the very first population is silent.
        cold_start = not self._types_seen
        changed = rolled_over

        for craft in aircraft_frame:
            hex_id = craft.get("hex")
            if hex_id and hex_id not in self._hexes_today:
                self._hexes_today.add(hex_id)
                changed = True

            ac_type = craft.get("type")
            if ac_type and ac_type not in self._types_seen:
                self._types_seen[ac_type] = dt_util.now().isoformat()
                changed = True
                if not cold_start:
                    self._hass.bus.async_fire(
                        EVENT_RARE_AIRCRAFT,
                        {
                            "aircraft": craft,
                            "entry_id": self._entry.entry_id,
                            "first_seen": True,
                        },
                    )

        if changed:
            self._schedule_save()

    @property
    def flyovers_today(self) -> int:
        """Number of flyover events recorded so far today."""
        return self._flyovers

    @property
    def unique_aircraft_today(self) -> int:
        """Number of distinct airframes (hexes) seen today."""
        return len(self._hexes_today)

    @property
    def busiest_hour(self) -> str | None:
        """Local hour with the most flyovers today, formatted "HH:00".

        None when no flyovers have happened today. Ties resolve to the
        earliest hour.
        """
        if not self._hour_counts:
            return None
        best_hour = min(
            self._hour_counts.items(), key=lambda item: (-item[1], int(item[0]))
        )[0]
        return f"{int(best_hour):02d}:00"

    @property
    def hour_counts(self) -> dict[str, int]:
        """Flyover count per local hour today, keyed by hour string ("0".."23")."""
        return dict(self._hour_counts)

    @property
    def types_seen_count(self) -> int:
        """Number of distinct aircraft types ever seen (all-time)."""
        return len(self._types_seen)
