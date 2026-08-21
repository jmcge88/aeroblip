"""Event platform for the Aeroblip integration.

Mirrors the coordinator's flyover/emergency notifications as Home Assistant
event entities, so automations can trigger on them without listening on the
HA event bus directly.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AeroblipCoordinator
from .entity import AeroblipEntity


def _trim(aircraft: dict[str, Any]) -> dict[str, Any]:
    """Strip an aircraft dict down to recorder-safe event attributes.

    Event entity attributes are written to the recorder on every firing, so
    the raw aircraft dict - which can carry nested info/photo blobs - must
    never be attached directly to an event.
    """
    route = aircraft.get("route") or {}
    airline = route.get("airline")
    if airline is None:
        # Fall back to the top-level airline dict when there's no route
        # (e.g. an emergency alert with no matched route). Its name lives
        # under "airline", mirroring the route dict's key.
        airline_info = aircraft.get("airline") or {}
        airline = airline_info.get("airline")

    trimmed: dict[str, Any] = {
        "callsign": aircraft.get("callsign"),
        "registration": aircraft.get("registration"),
        "aircraft_type": aircraft.get("type"),
        "description": aircraft.get("description"),
        "altitude_ft": aircraft.get("altitude_ft"),
        "ground_speed_kt": aircraft.get("ground_speed_kt"),
        "distance_nm": aircraft.get("distance_nm"),
        "phase": aircraft.get("phase"),
        "squawk": aircraft.get("squawk"),
        "origin": route.get("origin"),
        "origin_name": route.get("origin_name"),
        "destination": route.get("destination"),
        "destination_name": route.get("destination_name"),
        "airline": airline,
    }
    if "place" in aircraft:
        # Only alert (emergency) payloads carry this field.
        trimmed["place"] = aircraft.get("place")
    return trimmed


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aeroblip event entities from a config entry."""
    coordinator: AeroblipCoordinator = entry.runtime_data
    async_add_entities(
        [
            AeroblipFlyoverEvent(coordinator),
            AeroblipFlyoverImminentEvent(coordinator),
            AeroblipEmergencyEvent(coordinator),
        ]
    )


class AeroblipFlyoverEvent(AeroblipEntity, EventEntity):
    """Fires each time an aircraft newly enters the overhead radius."""

    _attr_translation_key = "flyover"
    _attr_event_types = ["flyover"]

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        """Initialize the flyover event entity."""
        super().__init__(coordinator, "flyover")

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's flyover listener."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_flyover_listener(self._handle))

    @callback
    def _handle(self, aircraft: dict[str, Any]) -> None:
        """Trigger the event entity from a fresh flyover aircraft dict."""
        self._trigger_event("flyover", _trim(aircraft))
        self.async_write_ha_state()


class AeroblipFlyoverImminentEvent(AeroblipEntity, EventEntity):
    """Fires once per approach when an aircraft is projected to be overhead soon."""

    _attr_translation_key = "flyover_imminent"
    _attr_event_types = ["flyover_imminent"]

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        """Initialize the flyover-imminent event entity."""
        super().__init__(coordinator, "flyover_imminent")

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's imminent-flyover listener."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_imminent_listener(self._handle))

    @callback
    def _handle(self, aircraft: dict[str, Any], eta_s: float) -> None:
        """Trigger the event entity from a projected aircraft dict and its ETA."""
        self._trigger_event(
            "flyover_imminent", {**_trim(aircraft), "eta_seconds": round(eta_s)}
        )
        self.async_write_ha_state()


class AeroblipEmergencyEvent(AeroblipEntity, EventEntity):
    """Fires each time an aircraft newly appears in the alerts feed."""

    _attr_translation_key = "emergency"
    _attr_event_types = ["emergency"]

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        """Initialize the emergency event entity."""
        super().__init__(coordinator, "emergency")

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's emergency listener."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_emergency_listener(self._handle))

    @callback
    def _handle(self, aircraft: dict[str, Any]) -> None:
        """Trigger the event entity from a fresh emergency aircraft dict."""
        self._trigger_event("emergency", _trim(aircraft))
        self.async_write_ha_state()
