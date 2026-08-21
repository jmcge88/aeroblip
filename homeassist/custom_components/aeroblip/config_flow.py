"""Config flow for the Aeroblip integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import AeroblipAuthError, AeroblipClient, AeroblipConnectionError
from .const import (
    CONF_AIRPORT,
    CONF_AREA_NM,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    CONF_RADIUS_NM,
    DEFAULT_AREA_NM,
    DEFAULT_RADIUS_NM,
    DOMAIN,
    MAX_AREA_NM,
    MAX_RADIUS_NM,
    MIN_AREA_NM,
    MIN_RADIUS_NM,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_base_url(raw: str) -> str:
    """Strip whitespace/trailing slash and default to http:// with no scheme."""
    url = raw.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    return url


def _radius_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_RADIUS_NM, max=MAX_RADIUS_NM, step=0.5, mode=NumberSelectorMode.BOX
        )
    )


def _area_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_AREA_NM, max=MAX_AREA_NM, step=1, mode=NumberSelectorMode.BOX
        )
    )


def _user_schema(default_latitude: float, default_longitude: float) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): str,
            vol.Optional(CONF_DEVICE_TOKEN): str,
            vol.Required(CONF_LATITUDE, default=default_latitude): vol.Coerce(float),
            vol.Required(CONF_LONGITUDE, default=default_longitude): vol.Coerce(float),
            vol.Required(CONF_RADIUS_NM, default=DEFAULT_RADIUS_NM): _radius_selector(),
            vol.Required(CONF_AREA_NM, default=DEFAULT_AREA_NM): _area_selector(),
            vol.Optional(CONF_AIRPORT, default=""): str,
        }
    )


async def _async_validate(
    hass: Any,
    *,
    base_url: str,
    device_token: str | None,
    latitude: float,
    longitude: float,
    radius_nm: float,
    area_nm: float,
    airport: str,
) -> str | None:
    """Validate connectivity/auth and return an error key, or None on success."""
    client = AeroblipClient(
        async_get_clientsession(hass),
        base_url,
        device_token=device_token,
        latitude=latitude,
        longitude=longitude,
        radius_nm=radius_nm,
        area_nm=area_nm,
        airport=airport,
    )
    try:
        await client.async_validate()
    except AeroblipAuthError:
        return "invalid_auth"
    except AeroblipConnectionError:
        return "cannot_connect"
    except Exception:  # noqa: BLE001 - guard the flow against any client bug
        _LOGGER.exception("Unexpected error validating Aeroblip connection")
        return "unknown"
    return None


class AeroblipConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aeroblip."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step: server address, location, and area."""
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = _normalize_base_url(user_input[CONF_BASE_URL])
            airport = user_input[CONF_AIRPORT].strip().upper()
            device_token = user_input.get(CONF_DEVICE_TOKEN) or None
            latitude = user_input[CONF_LATITUDE]
            longitude = user_input[CONF_LONGITUDE]
            radius_nm = user_input[CONF_RADIUS_NM]
            area_nm = user_input[CONF_AREA_NM]

            self._async_abort_entries_match(
                {
                    CONF_BASE_URL: base_url,
                    CONF_LATITUDE: latitude,
                    CONF_LONGITUDE: longitude,
                }
            )

            error = await _async_validate(
                self.hass,
                base_url=base_url,
                device_token=device_token,
                latitude=latitude,
                longitude=longitude,
                radius_nm=radius_nm,
                area_nm=area_nm,
                airport=airport,
            )
            if error is not None:
                errors["base"] = error
            else:
                title = (
                    f"Aeroblip ({airport})"
                    if airport
                    else f"Aeroblip ({urlparse(base_url).netloc})"
                )
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_BASE_URL: base_url,
                        CONF_DEVICE_TOKEN: device_token,
                        CONF_LATITUDE: latitude,
                        CONF_LONGITUDE: longitude,
                        CONF_RADIUS_NM: radius_nm,
                        CONF_AREA_NM: area_nm,
                        CONF_AIRPORT: airport,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(
                self.hass.config.latitude, self.hass.config.longitude
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the server rejects the device token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh device token and revalidate against the existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            device_token = user_input[CONF_DEVICE_TOKEN] or None
            data = entry.data
            error = await _async_validate(
                self.hass,
                base_url=data[CONF_BASE_URL],
                device_token=device_token,
                latitude=data[CONF_LATITUDE],
                longitude=data[CONF_LONGITUDE],
                radius_nm=data[CONF_RADIUS_NM],
                area_nm=data[CONF_AREA_NM],
                airport=data[CONF_AIRPORT],
            )
            if error is not None:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_DEVICE_TOKEN: device_token}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_TOKEN): str}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> AeroblipOptionsFlow:
        """Return the options flow for this config entry."""
        return AeroblipOptionsFlow()


class AeroblipOptionsFlow(OptionsFlow):
    """Handle Aeroblip options: radius, area, and airport only.

    Base URL/token/location aren't editable here - changing the server or
    home location is a new entry, not an option.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and process the single options step."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_RADIUS_NM: user_input[CONF_RADIUS_NM],
                    CONF_AREA_NM: user_input[CONF_AREA_NM],
                    CONF_AIRPORT: user_input[CONF_AIRPORT].strip().upper(),
                }
            )

        settings = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RADIUS_NM,
                        default=settings.get(CONF_RADIUS_NM, DEFAULT_RADIUS_NM),
                    ): _radius_selector(),
                    vol.Required(
                        CONF_AREA_NM,
                        default=settings.get(CONF_AREA_NM, DEFAULT_AREA_NM),
                    ): _area_selector(),
                    vol.Optional(
                        CONF_AIRPORT, default=settings.get(CONF_AIRPORT, "")
                    ): str,
                }
            ),
        )
