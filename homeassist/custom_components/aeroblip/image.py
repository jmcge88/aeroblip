"""Image entity for the Aeroblip integration.

Exposes a photo of the nearest tracked aircraft, sourced from the
coordinator's ``info.photo`` (falling back to ``info.photo_thumb``) via
the third-party photo host adsb.lol's overhead frame points at (e.g.
planespotters). ImageEntity's frontend cache-busts on
``image_last_updated``, so that timestamp is only bumped when the URL
actually changes - not on every ~5 s coordinator frame - to avoid
hammering the upstream photo host with a fresh fetch every frame for a
photo that hasn't changed.
"""
from __future__ import annotations

from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AeroblipConfigEntry
from .coordinator import AeroblipCoordinator
from .entity import AeroblipEntity


class AeroblipNearestAircraftImage(AeroblipEntity, ImageEntity):
    """Photo of the nearest tracked aircraft."""

    _attr_translation_key = "nearest_aircraft_photo"

    def __init__(self, hass: HomeAssistant, coordinator: AeroblipCoordinator) -> None:
        """Initialize the image entity and seed it from current coordinator data."""
        AeroblipEntity.__init__(self, coordinator, "nearest_aircraft_photo")
        ImageEntity.__init__(self, hass)
        self._attr_image_url = self._current_url()
        if self._attr_image_url is not None:
            self._attr_image_last_updated = dt_util.utcnow()

    def _nearest(self) -> dict[str, Any] | None:
        overhead = self.coordinator.data.overhead
        if not overhead:
            return None
        aircraft = overhead.get("aircraft") or []
        return aircraft[0] if aircraft else None

    def _current_url(self) -> str | None:
        nearest = self._nearest()
        if nearest is None:
            return None
        info = nearest.get("info") or {}
        return info.get("photo") or info.get("photo_thumb")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update the image URL only when it changes.

        The coordinator republishes a new snapshot on every WebSocket
        frame (~5 s), but the nearest aircraft's photo rarely changes
        between frames. Bumping ``image_last_updated`` unconditionally
        would cache-bust the frontend - and re-fetch from the upstream
        photo host - on every frame for no reason, so it's only touched
        when the URL itself actually differs from what's already set.
        """
        url = self._current_url()
        if url != self._attr_image_url:
            self._attr_image_url = url
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        nearest = self._nearest()
        if nearest is None or self._attr_image_url is None:
            return None
        return {
            "callsign": nearest.get("callsign"),
            "registration": nearest.get("registration"),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AeroblipConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aeroblip image entity from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([AeroblipNearestAircraftImage(hass, coordinator)])
