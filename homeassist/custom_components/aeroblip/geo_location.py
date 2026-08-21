"""Geolocation platform for the Aeroblip integration.

Every aircraft currently tracked inside the configured area radius is
surfaced as a transient geo_location event so it appears as a moving marker
on the Home Assistant map. Unlike the other platforms, these entities do not
derive from AeroblipEntity - see AeroblipGeolocationEvent for why.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTRIBUTION, DOMAIN, NM_TO_KM
from .coordinator import AeroblipCoordinator

_LOGGER = logging.getLogger(__name__)

# mdi:airplane, pointing due north in a 24x24 viewBox, so rotating it by the
# aircraft's track makes the marker point the way the plane is flying.
_PLANE_PATH = (
    "M21,16V14L13,9V3.5A1.5,1.5 0 0,0 11.5,2A1.5,1.5 0 0,0 10,3.5V9"
    "L2,14V16L10,13.5V19L8,20.5V22L11.5,21L15,22V20.5L13,19V13.5L21,16Z"
)


def _fmt_altitude(altitude_ft: Any) -> str | None:
    if not isinstance(altitude_ft, (int, float)):
        return None
    # k-notation above 10,000 ft: "34k ft" fits the marker, "34,000 ft" doesn't
    if altitude_ft >= 10000:
        return f"{altitude_ft / 1000:.0f}k ft"
    return f"{altitude_ft:.0f} ft"


def _marker_picture(
    name: str,
    track: float | None,
    overhead: bool,
    altitude_ft: Any = None,
    speed_kt: Any = None,
) -> str:
    """Inline-SVG map marker: a plane rotated to its heading, altitude above,
    callsign initial and ground speed below. The map card renders
    entity_picture as the marker, replacing its default letter-in-a-circle."""
    colour = "#e53935" if overhead else "#0288d1"
    # Radar feeds occasionally emit junk in track; never let a bad value
    # break marker rendering for the whole sync pass.
    rotate = (
        f' transform="rotate({track:.0f},12,12)"'
        if isinstance(track, (int, float))
        else ""
    )
    letter = (name or "?")[0].upper()
    top = _fmt_altitude(altitude_ft) or ""
    bottom = letter
    if isinstance(speed_kt, (int, float)):
        bottom = f"{letter}·{speed_kt:.0f}kt"
    # Transparent canvas: a solid colour disc with a white plane makes a
    # bigger, bolder marker than a thin ring, and the alt/speed labels sit
    # outside it with a dark halo so they stay readable on any map tile.
    label = (
        'text-anchor="middle" font-family="sans-serif" font-size="9" '
        'font-weight="700" fill="#fff" stroke="#1a1a1a" stroke-width="2.4" '
        'paint-order="stroke" stroke-linejoin="round"'
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
        f'<text x="24" y="10" {label}>{top}</text>'
        f'<circle cx="24" cy="24" r="13" fill="{colour}"/>'
        '<g transform="translate(24,24) scale(0.9) translate(-12,-12)">'
        f'<path{rotate} d="{_PLANE_PATH}" fill="#fff"/></g>'
        f'<text x="24" y="44" {label}>{bottom}</text>'
        "</svg>"
    )
    return "data:image/svg+xml," + quote(svg, safe="")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aeroblip geo_location entities from a config entry."""
    coordinator: AeroblipCoordinator = entry.runtime_data
    _LOGGER.info("aeroblip geo_location setup (marker style v4: disc + halo labels)")
    # Keyed on (kind, hex): nearby traffic and worldwide 7700 alerts are two
    # separate map sources, and one airframe may legitimately appear in both.
    tracked: dict[tuple[str, str], AeroblipGeolocationEvent] = {}

    def _placeable(aircraft: dict[str, Any]) -> bool:
        # Can't place an aircraft on the map without a position and distance.
        return (
            aircraft.get("hex") is not None
            and aircraft.get("lat") is not None
            and aircraft.get("lon") is not None
            and aircraft.get("distance_nm") is not None
        )

    @callback
    def _sync() -> None:
        """Reconcile tracked geo_location entities against the latest snapshot."""
        current: dict[tuple[str, str], dict[str, Any]] = {}
        if coordinator.data.connected:
            for aircraft in coordinator.data.overhead.get("aircraft") or []:
                if _placeable(aircraft):
                    current[("nearby", aircraft["hex"])] = aircraft
            for aircraft in coordinator.data.alerts.get("aircraft") or []:
                if _placeable(aircraft):
                    current[("emergency", aircraft["hex"])] = aircraft
                else:
                    # Alerts are rare and users notice a missing marker, so
                    # say exactly why one was dropped (MLAT-less feeds can
                    # report a 7700 with no position).
                    _LOGGER.warning(
                        "7700 alert %s has no usable position "
                        "(lat=%s lon=%s distance_nm=%s); not shown on map",
                        aircraft.get("callsign") or aircraft.get("hex"),
                        aircraft.get("lat"),
                        aircraft.get("lon"),
                        aircraft.get("distance_nm"),
                    )
        # else: the snapshot is retained across a WebSocket outage, but the
        # positions in it go stale within seconds - a marker 60 km off is
        # worse than no marker, so leave `current` empty to purge them all.

        new_entities: list[AeroblipGeolocationEvent] = []
        for key, aircraft in current.items():
            entity = tracked.get(key)
            if entity is None:
                entity = AeroblipGeolocationEvent(
                    aircraft, emergency=key[0] == "emergency"
                )
                _LOGGER.info(
                    "new marker %s alt=%s gs=%s svg=%.90s",
                    entity.name,
                    aircraft.get("altitude_ft"),
                    aircraft.get("ground_speed_kt"),
                    entity.entity_picture,
                )
                tracked[key] = entity
                new_entities.append(entity)
            else:
                entity.async_update_from_aircraft(aircraft)
        if new_entities:
            async_add_entities(new_entities)

        for key in list(tracked):
            if key not in current:
                tracked.pop(key).async_remove_self()

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    # Anything already in the snapshot at setup time (e.g. platform reload
    # while aircraft are overhead) should show up immediately, not wait for
    # the next WS frame.
    _sync()


class AeroblipGeolocationEvent(GeolocationEvent):
    """A single aircraft, represented as a transient geo_location marker.

    Deliberately NOT an AeroblipEntity and deliberately has no unique_id:
    geo_location entities are inherently transient (one per aircraft
    currently overhead), and assigning a stable unique_id per airframe hex
    would permanently register every plane that ever flew past in the
    entity registry.
    """

    _attr_should_poll = False
    _attr_source = DOMAIN
    _attr_attribution = ATTRIBUTION
    _attr_icon = "mdi:airplane"
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    # The marker SVG is a data URI regenerated every frame; recording ~800
    # bytes per plane every 5 s would bloat the database for no replay value.
    _unrecorded_attributes = frozenset({"entity_picture"})

    def __init__(self, aircraft: dict[str, Any], *, emergency: bool = False) -> None:
        """Initialize the event entity from the aircraft's first snapshot."""
        # Emergencies get their own source so a dashboard can put worldwide
        # 7700 traffic on a separate map from local nearby traffic.
        self._emergency = emergency
        self._attr_source = f"{DOMAIN}_emergency" if emergency else DOMAIN
        self._update_state(aircraft)

    @callback
    def async_update_from_aircraft(self, aircraft: dict[str, Any]) -> None:
        """Refresh position and attributes from a fresh aircraft dict."""
        self._update_state(aircraft)
        self.async_write_ha_state()

    def _update_state(self, aircraft: dict[str, Any]) -> None:
        """Recompute all entity state from an aircraft dict."""
        self._attr_name = (
            aircraft.get("callsign") or aircraft.get("registration") or aircraft["hex"]
        )
        self._attr_entity_picture = _marker_picture(
            self._attr_name,
            aircraft.get("track"),
            self._emergency or bool(aircraft.get("overhead")),
            altitude_ft=aircraft.get("altitude_ft"),
            speed_kt=aircraft.get("ground_speed_kt"),
        )
        self._attr_latitude = aircraft.get("lat")
        self._attr_longitude = aircraft.get("lon")
        distance_nm = aircraft.get("distance_nm")
        self._attr_distance = (
            round(distance_nm * NM_TO_KM, 1) if distance_nm is not None else None
        )
        route = aircraft.get("route") or {}
        self._attr_extra_state_attributes = {
            "callsign": aircraft.get("callsign"),
            "registration": aircraft.get("registration"),
            "aircraft_type": aircraft.get("type"),
            "altitude_ft": aircraft.get("altitude_ft"),
            "ground_speed_kt": aircraft.get("ground_speed_kt"),
            "track": aircraft.get("track"),
            "heading_cardinal": aircraft.get("heading_cardinal"),
            "phase": aircraft.get("phase"),
            "squawk": aircraft.get("squawk"),
            "overhead": aircraft.get("overhead"),
            "origin": route.get("origin"),
            "destination": route.get("destination"),
            "airline": route.get("airline"),
        }
        if self._emergency:
            self._attr_extra_state_attributes["place"] = aircraft.get("place")

    @callback
    def async_remove_self(self) -> None:
        """Schedule removal once this aircraft has vanished from the snapshot."""
        if self.hass is not None:
            self.hass.async_create_task(self.async_remove())

    async def async_will_remove_from_hass(self) -> None:
        """Clean up before removal.

        No timers or listeners are registered on the entity itself - the
        coordinator listener driving add/remove lives on the platform's
        _sync callback - so there is nothing extra to release here.
        """
