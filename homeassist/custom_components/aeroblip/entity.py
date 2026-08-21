"""Base entity for the Aeroblip integration.

All platform entities (binary_sensor, event, geo_location, sensor) derive
from this class so they share one unique-id scheme, one device, and one
availability rule tied to the WebSocket connection state.
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_BASE_URL, DOMAIN
from .coordinator import AeroblipCoordinator


class AeroblipEntity(CoordinatorEntity[AeroblipCoordinator]):
    """Common base for all Aeroblip entities."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: AeroblipCoordinator, key: str) -> None:
        """Initialize the entity with a unique id scoped to the config entry."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Aeroblip",
            configuration_url=entry.data[CONF_BASE_URL],
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Return False while the Aeroblip WebSocket connection is down.

        The coordinator never stops polling (it's push-mode), so the base
        CoordinatorEntity.available alone doesn't reflect a dropped link -
        entities must also check the latest connected flag pushed onto data.
        """
        return super().available and self.coordinator.data.connected
