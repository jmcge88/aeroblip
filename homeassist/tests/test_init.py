"""Tests for Aeroblip config entry setup/unload."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aeroblip.api import AeroblipAuthError, AeroblipConnectionError
from custom_components.aeroblip.const import DOMAIN
from custom_components.aeroblip.coordinator import AeroblipCoordinator

from conftest import ENTRY_DATA, KNOWN_ENTITY_IDS


async def test_setup_creates_coordinator_and_entities(hass, setup_entry):
    entry, _holder, _mock_stop = setup_entry

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, AeroblipCoordinator)

    for entity_id in KNOWN_ENTITY_IDS:
        assert hass.states.get(entity_id) is not None, f"missing entity {entity_id}"


async def test_setup_retries_on_connection_error(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=dict(ENTRY_DATA), title="Aeroblip (BNE)")
    entry.add_to_hass(hass)

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(side_effect=AeroblipConnectionError("boom")),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert result is False
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_errors_on_auth_error(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=dict(ENTRY_DATA), title="Aeroblip (BNE)")
    entry.add_to_hass(hass)

    with patch(
        "custom_components.aeroblip.api.AeroblipClient.async_validate",
        new=AsyncMock(side_effect=AeroblipAuthError("bad token")),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert result is False
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_unload_stops_client_and_marks_not_loaded(hass, setup_entry):
    entry, _holder, mock_stop = setup_entry

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert mock_stop.await_count >= 1
