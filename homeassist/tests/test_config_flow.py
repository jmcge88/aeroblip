"""Tests for the Aeroblip config and options flows."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aeroblip.api import AeroblipAuthError, AeroblipConnectionError
from custom_components.aeroblip.const import (
    CONF_AIRPORT,
    CONF_AREA_NM,
    CONF_BASE_URL,
    CONF_DEVICE_TOKEN,
    CONF_RADIUS_NM,
    DOMAIN,
)

from conftest import HEALTH, mock_ws_client

USER_INPUT = {
    CONF_BASE_URL: "192.168.1.10:8000 ",
    CONF_LATITUDE: -27.3842,
    CONF_LONGITUDE: 153.1175,
    CONF_RADIUS_NM: 5.0,
    CONF_AREA_NM: 60.0,
    CONF_AIRPORT: "bne",
}


async def test_user_flow_happy_path(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # async_create_entry immediately triggers a real async_setup_entry for
    # the new entry, which would otherwise start a real WS connection -
    # async_run/async_stop need mocking too, not just async_validate.
    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(return_value=HEALTH),
    ), mock_ws_client():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Aeroblip (BNE)"
        assert result["data"][CONF_BASE_URL] == "http://192.168.1.10:8000"
        assert result["data"][CONF_AIRPORT] == "BNE"

        await hass.config_entries.async_unload(result["result"].entry_id)
        await hass.async_block_till_done()


async def test_user_flow_cannot_connect_then_retry_succeeds(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(side_effect=AeroblipConnectionError("boom")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(return_value=HEALTH),
    ), mock_ws_client():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY

        await hass.config_entries.async_unload(result["result"].entry_id)
        await hass.async_block_till_done()


async def test_user_flow_invalid_auth(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(side_effect=AeroblipAuthError("bad token")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_duplicate_aborts(hass):
    MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "http://192.168.1.10:8000",
            CONF_DEVICE_TOKEN: None,
            CONF_LATITUDE: -27.3842,
            CONF_LONGITUDE: 153.1175,
            CONF_RADIUS_NM: 5.0,
            CONF_AREA_NM: 60.0,
            CONF_AIRPORT: "BNE",
        },
        title="Aeroblip (BNE)",
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    # No client patch needed: the duplicate check happens before validation.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_updates_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BASE_URL: "http://192.168.1.10:8000",
            CONF_DEVICE_TOKEN: None,
            CONF_LATITUDE: -27.3842,
            CONF_LONGITUDE: 153.1175,
            CONF_RADIUS_NM: 5.0,
            CONF_AREA_NM: 60.0,
            CONF_AIRPORT: "BNE",
        },
        title="Aeroblip (BNE)",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RADIUS_NM: 10.0, CONF_AREA_NM: 100.0, CONF_AIRPORT: "ybbn"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_RADIUS_NM] == 10.0
    assert entry.options[CONF_AREA_NM] == 100.0
    assert entry.options[CONF_AIRPORT] == "YBBN"
