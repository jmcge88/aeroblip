"""Sensor entities for the Aeroblip integration.

Seven sensors surface the coordinator's three data buckets (overhead,
board, alerts) as HA state. All are defensive against the pre-first-frame
``{}`` snapshot and against any individual field being ``None`` - this
integration is fed live, occasionally messy, third-party flight data.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import DEGREE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AeroblipConfigEntry
from .coordinator import AeroblipCoordinator
from .entity import AeroblipEntity
from .watch import AeroblipWatchManager

# 16-point compass rose, N first, each sector 22.5 deg wide, centred on the
# point (i.e. the boundary between N and NNE is 11.25 deg, not 22.5 deg).
_CARDINAL_POINTS: tuple[str, ...] = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def _cardinal(bearing: float) -> str:
    """Return the 16-point compass cardinal for a bearing in degrees."""
    index = round(bearing / 22.5) % 16
    return _CARDINAL_POINTS[index]


def _parse_board_time(value: str | None) -> datetime | None:
    """Parse a board row's ISO timestamp, localising naive values.

    The Aeroblip server emits the airport's local time and does not
    guarantee a UTC offset is present, so a naive result is assumed to
    already be in HA's configured local time zone.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return parsed


def _effective_board_time(row: dict[str, Any]) -> datetime | None:
    """Return a board row's best-known time: estimated, falling back to scheduled."""
    return _parse_board_time(row.get("estimated")) or _parse_board_time(
        row.get("scheduled")
    )


def _next_board_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the soonest upcoming, non-cancelled row with a parseable time.

    Rows are not guaranteed sorted, so this scans the full list. "Soonest
    upcoming" allows a 5-minute grace window behind now to tolerate clock
    skew and rows that just departed/arrived.
    """
    cutoff = dt_util.now() - timedelta(minutes=5)
    best_row: dict[str, Any] | None = None
    best_time: datetime | None = None
    for row in rows:
        status = row.get("status") or ""
        if "cancel" in status.lower():
            continue
        effective = _effective_board_time(row)
        if effective is None or effective < cutoff:
            continue
        if best_time is None or effective < best_time:
            best_time = effective
            best_row = row
    return best_row


def _alert_summary(aircraft: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce raw alert aircraft dicts to the small attrs the frontend needs."""
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


class AeroblipOverheadCountSensor(AeroblipEntity, SensorEntity):
    """Number of aircraft currently inside the overhead radius."""

    _attr_translation_key = "overhead_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "overhead_count")

    @property
    def native_value(self) -> int | None:
        overhead = self.coordinator.data.overhead
        if not overhead or overhead.get("updated") is None:
            return None
        return overhead.get("overhead_count")


class AeroblipNearbyCountSensor(AeroblipEntity, SensorEntity):
    """Number of aircraft currently inside the wider area radius."""

    _attr_translation_key = "nearby_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "nearby_count")

    @property
    def native_value(self) -> int | None:
        overhead = self.coordinator.data.overhead
        if not overhead or overhead.get("updated") is None:
            return None
        return len(overhead.get("aircraft") or [])


class AeroblipNearestAircraftSensor(AeroblipEntity, SensorEntity):
    """Identity and detail of the nearest tracked aircraft."""

    _attr_translation_key = "nearest_aircraft"
    # photo is a large/opaque URL and description churns with every frame;
    # neither is useful in long-term state history.
    _unrecorded_attributes = frozenset({"photo", "description"})

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "nearest_aircraft")

    def _nearest(self) -> dict[str, Any] | None:
        overhead = self.coordinator.data.overhead
        if not overhead:
            return None
        aircraft = overhead.get("aircraft") or []
        return aircraft[0] if aircraft else None

    @property
    def native_value(self) -> str | None:
        nearest = self._nearest()
        if nearest is None:
            return None
        return nearest.get("callsign") or nearest.get("registration") or nearest.get("hex")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        nearest = self._nearest()
        if nearest is None:
            return None
        route = nearest.get("route") or {}
        airline = nearest.get("airline") or {}
        info = nearest.get("info") or {}
        return {
            "callsign": nearest.get("callsign"),
            "registration": nearest.get("registration"),
            "aircraft_type": nearest.get("type"),
            "description": nearest.get("description"),
            "altitude_ft": nearest.get("altitude_ft"),
            "ground_speed_kt": nearest.get("ground_speed_kt"),
            "distance_nm": nearest.get("distance_nm"),
            "bearing_from_home": nearest.get("bearing_from_home"),
            "heading_cardinal": nearest.get("heading_cardinal"),
            "phase": nearest.get("phase"),
            "origin": route.get("origin"),
            "origin_name": route.get("origin_name"),
            "destination": route.get("destination"),
            "destination_name": route.get("destination_name"),
            "airline": route.get("airline") or airline.get("airline"),
            "photo": info.get("photo"),
        }


class AeroblipNearestBearingSensor(AeroblipEntity, SensorEntity):
    """Bearing from home to the nearest tracked aircraft."""

    _attr_translation_key = "nearest_bearing"
    _attr_native_unit_of_measurement = DEGREE
    _attr_icon = "mdi:compass-rose"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "nearest_bearing")

    @property
    def suggested_object_id(self) -> str | None:
        """Pin the object id to "nearest_bearing".

        strings.json's display name ("Nearest aircraft bearing") is more
        descriptive than the slug we want in the entity_id, so the default
        name-derived suggestion is overridden here rather than by editing
        the display name itself.
        """
        return "nearest_bearing"

    def _nearest(self) -> dict[str, Any] | None:
        overhead = self.coordinator.data.overhead
        if not overhead:
            return None
        aircraft = overhead.get("aircraft") or []
        return aircraft[0] if aircraft else None

    @property
    def native_value(self) -> float | None:
        nearest = self._nearest()
        if nearest is None:
            return None
        return nearest.get("bearing_from_home")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        nearest = self._nearest()
        if nearest is None:
            return None
        bearing = nearest.get("bearing_from_home")
        return {
            "callsign": nearest.get("callsign"),
            "cardinal": _cardinal(bearing) if bearing is not None else None,
        }


class AeroblipNextFlyoverSensor(AeroblipEntity, SensorEntity):
    """Predicted time the soonest-projected aircraft enters the overhead ring."""

    _attr_translation_key = "next_flyover"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "next_flyover")

    @property
    def native_value(self) -> datetime | None:
        prediction = self.coordinator.data.prediction
        if prediction is None:
            return None
        # Round down to a 5 s boundary so the state - and therefore the
        # recorder - doesn't churn every single ~5 s frame as the ETA drifts
        # by fractions of a second.
        enters_at = prediction["enters_at"]
        enters_at -= enters_at % 5
        return dt_util.utc_from_timestamp(enters_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        prediction = self.coordinator.data.prediction
        if prediction is None:
            return None
        aircraft = prediction["aircraft"]
        route = aircraft.get("route") or {}
        airline = aircraft.get("airline") or {}
        return {
            "callsign": aircraft.get("callsign"),
            "airline": route.get("airline") or airline.get("airline"),
            "origin": route.get("origin"),
            "destination": route.get("destination"),
            "aircraft_type": aircraft.get("type"),
            "distance_nm": aircraft.get("distance_nm"),
            "eta_seconds": round(prediction["eta_s"]),
        }


@dataclass(frozen=True, kw_only=True)
class _BoardSensorDescription(SensorEntityDescription):
    """Entity description for a board (arrivals/departures) sensor."""

    rows_fn: Callable[[dict[str, Any]], list[dict[str, Any]]] = lambda board: []


class AeroblipBoardSensor(AeroblipEntity, SensorEntity):
    """Base for the next-arrival / next-departure sensors."""

    entity_description: _BoardSensorDescription

    def __init__(
        self, coordinator: AeroblipCoordinator, description: _BoardSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.key

    def _next_row(self) -> dict[str, Any] | None:
        board = self.coordinator.data.board
        if not board:
            return None
        rows = self.entity_description.rows_fn(board)
        return _next_board_row(rows)

    @property
    def native_value(self) -> str | None:
        row = self._next_row()
        if row is None:
            return None
        return row.get("flight")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        row = self._next_row()
        if row is None:
            return None
        return {
            "airline": row.get("airline"),
            "city": row.get("city"),
            "code": row.get("code"),
            "scheduled": row.get("scheduled"),
            "estimated": row.get("estimated"),
            "terminal": row.get("terminal"),
            "gate": row.get("gate"),
            "status": row.get("status"),
            "aircraft_type": row.get("aircraft"),
        }


class AeroblipAlertCountSensor(AeroblipEntity, SensorEntity):
    """Count of active emergency-squawk aircraft."""

    _attr_translation_key = "alert_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "alert_count")

    @property
    def native_value(self) -> int | None:
        alerts = self.coordinator.data.alerts
        if not alerts:
            return None
        return alerts.get("count")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        alerts = self.coordinator.data.alerts
        if not alerts:
            return None
        return {"alerts": _alert_summary(alerts.get("aircraft") or [])}


class AeroblipProviderSensor(AeroblipEntity, SensorEntity):
    """Diagnostic sensor reporting which ADS-B data provider is in use."""

    _attr_translation_key = "provider"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "provider")

    @property
    def native_value(self) -> str | None:
        overhead = self.coordinator.data.overhead
        if not overhead:
            return None
        return overhead.get("provider")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        overhead = self.coordinator.data.overhead
        updated = overhead.get("updated") if overhead else None
        if updated is None:
            return None
        return {"updated": dt_util.utc_from_timestamp(updated).isoformat()}


class AeroblipFlyoversTodaySensor(AeroblipEntity, SensorEntity):
    """Count of flyover events recorded so far today (from AeroblipStats)."""

    _attr_translation_key = "flyovers_today"
    _attr_icon = "mdi:counter"
    # TOTAL (not TOTAL_INCREASING) because the count resets at local
    # midnight rather than ever-increasing like a meter; last_reset below
    # tells long-term statistics that reset is a fresh accumulation, not a
    # rollover to be diffed against the previous value.
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "flyovers_today")

    @property
    def native_value(self) -> int:
        return self.coordinator.stats.flyovers_today

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day()

    @property
    def available(self) -> bool:
        """Stats survive a WebSocket outage, so bypass the connected gate.

        Same reasoning as AeroblipConnectedSensor in binary_sensor.py:
        AeroblipEntity.available would go False whenever the link drops,
        but this entity's data doesn't go stale when that happens.
        """
        return self.coordinator.last_update_success


class AeroblipUniqueAircraftTodaySensor(AeroblipEntity, SensorEntity):
    """Count of distinct airframes spotted so far today (from AeroblipStats)."""

    _attr_translation_key = "unique_aircraft_today"
    _attr_icon = "mdi:airplane-search"
    # See AeroblipFlyoversTodaySensor for why TOTAL + last_reset, not
    # TOTAL_INCREASING or MEASUREMENT.
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "unique_aircraft_today")

    @property
    def native_value(self) -> int:
        return self.coordinator.stats.unique_aircraft_today

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"types_seen_all_time": self.coordinator.stats.types_seen_count}

    @property
    def available(self) -> bool:
        """See AeroblipFlyoversTodaySensor.available."""
        return self.coordinator.last_update_success


class AeroblipBusiestHourSensor(AeroblipEntity, SensorEntity):
    """Local hour with the most flyovers recorded so far today."""

    _attr_translation_key = "busiest_hour"
    # mdi:clock-star-four-points-outline is not a real MDI icon name;
    # falling back to a plain clock per the spec's own fallback guidance.
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: AeroblipCoordinator) -> None:
        super().__init__(coordinator, "busiest_hour")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.stats.busiest_hour

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"hour_counts": self.coordinator.stats.hour_counts}

    @property
    def available(self) -> bool:
        """See AeroblipFlyoversTodaySensor.available."""
        return self.coordinator.last_update_success


class AeroblipWatchSensor(AeroblipEntity, SensorEntity):
    """Live tracking status for one user-requested flight watch.

    Unlike every other sensor in this module, its name is per-callsign and
    therefore dynamic rather than translated, so _attr_has_entity_name
    (inherited True from AeroblipEntity) is turned off here and a plain
    _attr_name is set directly - a translation_key can't carry a runtime
    value like the watched callsign.
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:radar"

    def __init__(
        self,
        coordinator: AeroblipCoordinator,
        manager: AeroblipWatchManager,
        callsign: str,
    ) -> None:
        self.callsign = callsign
        self._manager = manager
        super().__init__(coordinator, f"watch_{callsign.lower()}")
        self._attr_name = f"Watch {callsign}"

    @property
    def native_value(self) -> str:
        status, _aircraft = self._manager.current(self.callsign)
        return status

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        _status, aircraft = self._manager.current(self.callsign)
        if aircraft is None:
            return None
        route = aircraft.get("route") or {}
        return {
            "callsign": aircraft.get("callsign"),
            "aircraft_type": aircraft.get("type"),
            "altitude_ft": aircraft.get("altitude_ft"),
            "ground_speed_kt": aircraft.get("ground_speed_kt"),
            "distance_nm": aircraft.get("distance_nm"),
            "phase": aircraft.get("phase"),
            "origin": route.get("origin"),
            "destination": route.get("destination"),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to the manager's per-watch updates and register for removal."""
        await super().async_added_to_hass()
        self._manager.register_entity(self.callsign, self)
        self.async_on_remove(
            self._manager.async_add_watch_listener(self.callsign, self._handle_watch_update)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Deregister from the manager so a later removal can't hold a stale reference."""
        self._manager.unregister_entity(self.callsign, self)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_watch_update(self, status: str, aircraft: dict[str, Any] | None) -> None:
        """Write fresh state on a watch transition.

        Belt-and-suspenders alongside the coordinator's own push - this
        entity is also a CoordinatorEntity and gets re-rendered on every
        WS frame regardless, but this guarantees a write happens exactly
        when the manager reports a transition even if that ordering ever
        changes.
        """
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AeroblipConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aeroblip sensor entities from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            AeroblipOverheadCountSensor(coordinator),
            AeroblipNearbyCountSensor(coordinator),
            AeroblipNearestAircraftSensor(coordinator),
            AeroblipNearestBearingSensor(coordinator),
            AeroblipNextFlyoverSensor(coordinator),
            AeroblipBoardSensor(
                coordinator,
                _BoardSensorDescription(
                    key="next_arrival",
                    rows_fn=lambda board: board.get("arrivals") or [],
                ),
            ),
            AeroblipBoardSensor(
                coordinator,
                _BoardSensorDescription(
                    key="next_departure",
                    rows_fn=lambda board: board.get("departures") or [],
                ),
            ),
            AeroblipAlertCountSensor(coordinator),
            AeroblipProviderSensor(coordinator),
            AeroblipFlyoversTodaySensor(coordinator),
            AeroblipUniqueAircraftTodaySensor(coordinator),
            AeroblipBusiestHourSensor(coordinator),
        ]
    )

    # Watches created before this platform loaded (e.g. persisted across a
    # restart) need their sensor recreated now; watches added later via the
    # watch_flight service arrive through _add_watch_sensor instead.
    manager: AeroblipWatchManager = coordinator.watch_manager  # type: ignore[attr-defined]
    if manager.watches:
        async_add_entities(
            [AeroblipWatchSensor(coordinator, manager, callsign) for callsign in manager.watches]
        )

    @callback
    def _add_watch_sensor(callsign: str) -> None:
        async_add_entities([AeroblipWatchSensor(coordinator, manager, callsign)])

    manager.set_add_entity_callback(_add_watch_sensor)
