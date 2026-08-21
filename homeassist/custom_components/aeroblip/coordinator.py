"""Push-mode data coordinator for the Aeroblip integration.

The Aeroblip server streams state over a single long-lived WebSocket
rather than answering polled requests, so this coordinator has no
update_interval - it is fed entirely by AeroblipClient.async_run callbacks
and republishes a fresh AeroblipData snapshot on every frame.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import AeroblipAuthError, AeroblipClient
from .const import (
    CONF_RADIUS_NM,
    DOMAIN,
    EVENT_EMERGENCY,
    EVENT_FLYOVER,
    EVENT_FLYOVER_IMMINENT,
    IMMINENT_SECONDS,
    PREDICTION_HORIZON_S,
)
from .prediction import seconds_until_overhead

_LOGGER = logging.getLogger(__name__)


@dataclass
class AeroblipData:
    """Latest snapshot pushed over the Aeroblip WebSocket."""

    overhead: dict[str, Any] = field(default_factory=dict)
    board: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    connected: bool = False
    prediction: dict[str, Any] | None = None


class AeroblipCoordinator(DataUpdateCoordinator[AeroblipData]):
    """Coordinator that fans out Aeroblip WebSocket frames to entities."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: AeroblipClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # push-mode: data arrives via the WS, not polling
            config_entry=entry,
        )
        self.client = client
        self.data = AeroblipData()
        self._overhead_hexes: set[str] = set()
        self._alert_hexes: set[str] = set()
        # A (re)connect must not replay flyover/emergency events for aircraft
        # that were already overhead/alerting before the reconnect happened.
        self._overhead_primed = False
        self._alerts_primed = False
        self._flyover_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._emergency_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._imminent_listeners: list[Callable[[dict[str, Any], float], None]] = []
        # Home position for flyover projection; options never change these,
        # so entry.data (not the merged options) is the right source.
        self._home_lat: float = entry.data[CONF_LATITUDE]
        self._home_lon: float = entry.data[CONF_LONGITUDE]
        # Radius fallback for frames that omit overhead_radius_nm; radius is
        # options-overridable (see __init__.py's settings merge), and an
        # options change reloads the entry, recreating this coordinator.
        self._default_radius_nm: float = entry.options.get(
            CONF_RADIUS_NM, entry.data[CONF_RADIUS_NM]
        )
        # Hexes already announced as imminent, so a still-approaching aircraft
        # isn't re-fired every ~5s frame until it actually goes overhead.
        self._imminent_hexes: set[str] = set()
        # Mirrors _overhead_primed: a (re)connect baseline must not fire
        # imminent events for aircraft already close on the first frame.
        self._prediction_primed = False

    def async_start(self) -> None:
        """Start the background task that runs the WebSocket loop."""
        self.config_entry.async_create_background_task(
            self.hass, self._run(), name="aeroblip-ws"
        )

    async def _run(self) -> None:
        """Run the client loop; hand a bad token off to HA's reauth flow."""
        try:
            await self.client.async_run(self._handle_message, self._handle_connection)
        except AeroblipAuthError:
            _LOGGER.info("Aeroblip device token rejected; starting reauth")
            self.config_entry.async_start_reauth(self.hass)

    async def async_shutdown_client(self) -> None:
        """Close the underlying WebSocket so the background task can be cancelled."""
        await self.client.async_stop()

    @callback
    def _handle_connection(self, connected: bool) -> None:
        if connected:
            # Next overhead/alerts frame is a fresh baseline, not new events.
            self._overhead_primed = False
            self._alerts_primed = False
            self._prediction_primed = False
        new_data = dataclasses.replace(self.data, connected=connected)
        self.async_set_updated_data(new_data)

    @callback
    def _handle_message(self, msg_type: str, data: dict[str, Any]) -> None:
        if msg_type == "overhead":
            prediction = self._process_overhead(data)
            new_data = dataclasses.replace(self.data, overhead=data, prediction=prediction)
        elif msg_type == "board":
            new_data = dataclasses.replace(self.data, board=data)
        elif msg_type == "alerts":
            self._process_alerts(data)
            new_data = dataclasses.replace(self.data, alerts=data)
        else:
            _LOGGER.debug("Ignoring unknown Aeroblip message type: %s", msg_type)
            return
        # Always a new object - entities compare references to detect change.
        self.async_set_updated_data(new_data)

    def _process_overhead(self, data: dict[str, Any]) -> dict[str, Any] | None:
        aircraft = data.get("aircraft") or []
        current = {a["hex"] for a in aircraft if a.get("overhead") and a.get("hex")}
        if self._overhead_primed:
            for hex_id in current - self._overhead_hexes:
                match = next((a for a in aircraft if a.get("hex") == hex_id), None)
                if match is not None:
                    self._fire_flyover(match)
        self._overhead_hexes = current
        self._overhead_primed = True
        return self._process_prediction(data, aircraft, current)

    def _process_prediction(
        self,
        data: dict[str, Any],
        aircraft: list[dict[str, Any]],
        overhead_hexes: set[str],
    ) -> dict[str, Any] | None:
        """Project every tracked aircraft and return the soonest overhead candidate.

        Also fires EVENT_FLYOVER_IMMINENT (once per hex, until it goes
        overhead or drops off the frame) for whichever candidate is close
        enough - separate from returning the soonest candidate as the
        published prediction, since the two can be the same aircraft on
        almost every frame but aren't guaranteed to be.
        """
        radius_nm = data.get("overhead_radius_nm") or self._default_radius_nm
        updated = data.get("updated")
        now = updated if updated is not None else time.time()

        best_hex: str | None = None
        best_eta: float | None = None
        best_aircraft: dict[str, Any] | None = None
        for candidate in aircraft:
            hex_id = candidate.get("hex")
            if not hex_id:
                continue
            eta = seconds_until_overhead(
                candidate, self._home_lat, self._home_lon, radius_nm, PREDICTION_HORIZON_S
            )
            if eta is None:
                continue
            if best_eta is None or eta < best_eta:
                best_hex, best_eta, best_aircraft = hex_id, eta, candidate

        present_hexes = {a.get("hex") for a in aircraft if a.get("hex")}
        # An announced hex stops being tracked once it's actually overhead
        # (the flyover event takes over from there) or vanishes from the
        # frame (out of range, lost contact), so a later re-approach can be
        # announced again.
        self._imminent_hexes &= present_hexes - overhead_hexes

        prediction: dict[str, Any] | None = None
        if best_hex is not None and best_eta is not None and best_aircraft is not None:
            prediction = {
                "eta_s": best_eta,
                "enters_at": now + best_eta,
                "aircraft": best_aircraft,
            }
            if (
                self._prediction_primed
                and best_eta <= IMMINENT_SECONDS
                and best_hex not in self._imminent_hexes
            ):
                self._imminent_hexes.add(best_hex)
                self._fire_imminent(best_aircraft, best_eta)

        self._prediction_primed = True
        return prediction

    def _process_alerts(self, data: dict[str, Any]) -> None:
        aircraft = data.get("aircraft") or []
        current = {a["hex"] for a in aircraft if a.get("hex")}
        if self._alerts_primed:
            for hex_id in current - self._alert_hexes:
                match = next((a for a in aircraft if a.get("hex") == hex_id), None)
                if match is not None:
                    self._fire_emergency(match)
        self._alert_hexes = current
        self._alerts_primed = True

    def _fire_flyover(self, aircraft: dict[str, Any]) -> None:
        self.hass.bus.async_fire(
            EVENT_FLYOVER,
            {"aircraft": aircraft, "entry_id": self.config_entry.entry_id},
        )
        for listener in list(self._flyover_listeners):
            listener(aircraft)

    def _fire_emergency(self, aircraft: dict[str, Any]) -> None:
        self.hass.bus.async_fire(
            EVENT_EMERGENCY,
            {"aircraft": aircraft, "entry_id": self.config_entry.entry_id},
        )
        for listener in list(self._emergency_listeners):
            listener(aircraft)

    def _fire_imminent(self, aircraft: dict[str, Any], eta_s: float) -> None:
        self.hass.bus.async_fire(
            EVENT_FLYOVER_IMMINENT,
            {
                "aircraft": aircraft,
                "entry_id": self.config_entry.entry_id,
                "eta_s": eta_s,
            },
        )
        for listener in list(self._imminent_listeners):
            listener(aircraft, eta_s)

    @callback
    def async_add_flyover_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> CALLBACK_TYPE:
        """Register a callback invoked with the aircraft dict on each new flyover."""
        self._flyover_listeners.append(listener)

        def _remove() -> None:
            if listener in self._flyover_listeners:
                self._flyover_listeners.remove(listener)

        return _remove

    @callback
    def async_add_emergency_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> CALLBACK_TYPE:
        """Register a callback invoked with the aircraft dict on each new emergency."""
        self._emergency_listeners.append(listener)

        def _remove() -> None:
            if listener in self._emergency_listeners:
                self._emergency_listeners.remove(listener)

        return _remove

    @callback
    def async_add_imminent_listener(
        self, listener: Callable[[dict[str, Any], float], None]
    ) -> CALLBACK_TYPE:
        """Register a callback invoked with (aircraft, eta_s) on each imminent flyover."""
        self._imminent_listeners.append(listener)

        def _remove() -> None:
            if listener in self._imminent_listeners:
                self._imminent_listeners.remove(listener)

        return _remove
