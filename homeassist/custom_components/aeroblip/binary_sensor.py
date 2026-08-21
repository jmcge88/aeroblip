"""Binary sensor entities for the Aeroblip integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AeroblipConfigEntry
from .coordinator import AeroblipCoordinator
from .entity import AeroblipEntity


def _alert_summary(aircraft: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce raw alert aircraft dicts to the small attrs the frontend needs.

    Duplicated from sensor.py rather than imported: the two platforms are
    siblings with no dependency relationship, and the helper is tiny.
    """
    return [
        {
            "callsign": a.get("callsign"),
            "registration": a.get("registration"),
            "type": a.get("type"),
            "place": a.get("place"),
            "distance_nm": a.get("distance_nm"),
            "squawk": a.get("squawk"),
        }
        for a in aircraft
    ]


class AeroblipFlightOverheadSensor(AeroblipEntity, BinarySensorEntity):
    """Whether any aircraft is currently inside the overhead radius."""

    _attr_translation_key = "flight_overhead"

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "flight_overhead")

    @property
    def is_on(self) -> bool:
        overhead = self.coordinator.data.overhead
        return bool(overhead.get("overhead_count", 0)) if overhead else False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        overhead = self.coordinator.data.overhead
        aircraft = overhead.get("aircraft") or [] if overhead else []
        overhead_aircraft = [a for a in aircraft if a.get("overhead")]
        summary = []
        for a in overhead_aircraft:
            route = a.get("route") or {}
            summary.append(
                {
                    "callsign": a.get("callsign"),
                    "registration": a.get("registration"),
                    "aircraft_type": a.get("type"),
                    "altitude_ft": a.get("altitude_ft"),
                    "distance_nm": a.get("distance_nm"),
                    "origin": route.get("origin"),
                    "destination": route.get("destination"),
                }
            )
        return {"aircraft": summary}


class AeroblipEmergencyActiveSensor(AeroblipEntity, BinarySensorEntity):
    """Whether any tracked aircraft is currently squawking an emergency code."""

    _attr_translation_key = "emergency_active"
    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "emergency_active")

    @property
    def is_on(self) -> bool:
        alerts = self.coordinator.data.alerts
        return bool(alerts.get("count", 0)) if alerts else False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.data.alerts
        aircraft = alerts.get("aircraft") or [] if alerts else []
        return {"alerts": _alert_summary(aircraft)}


class AeroblipConnectedSensor(AeroblipEntity, BinarySensorEntity):
    """Whether the Aeroblip server WebSocket is currently connected."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.connected

    @property
    def available(self) -> bool:
        """Bypass AeroblipEntity's connected-gated availability.

        AeroblipEntity.available is False whenever the WebSocket is down,
        which is exactly the state this entity exists to report - gating on
        it would make the entity unavailable precisely when it needs to
        show "off". Fall back to plain coordinator health instead.
        """
        return self.coordinator.last_update_success


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AeroblipConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aeroblip binary sensor entities from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            AeroblipFlightOverheadSensor(coordinator),
            AeroblipEmergencyActiveSensor(coordinator),
            AeroblipConnectedSensor(coordinator),
        ]
    )
