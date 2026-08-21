"""Tests for the Aeroblip image entity (custom_components/aeroblip/image.py).

ImageEntity's own state IS ``image_last_updated`` (its state property
returns ``image_last_updated.isoformat()``, or None -> "unknown" - see
homeassistant.components.image.ImageEntity.state), and its
``entity_picture`` attribute is always HA's own image-proxy URL
(``/api/image_proxy/<entity_id>?token=...``), never the raw upstream photo
URL - so "did the photo change" is asserted here via the state timestamp
(bumped only when the URL genuinely changes, per _handle_coordinator_update)
together with the entity's own extra_state_attributes (callsign/
registration), not via entity_picture's content.

Frames are injected the same way as test_coordinator.py: via
holder.on_message, followed by hass.async_block_till_done().
"""
from __future__ import annotations

import copy

from conftest import OVERHEAD, OVERHEAD_WITH_PHOTO

_IMAGE_ENTITY_ID = "image.aeroblip_bne_nearest_aircraft_photo"


async def test_image_entity_exists(hass, setup_entry):
    _entry, _holder, _mock_stop = setup_entry

    state = hass.states.get(_IMAGE_ENTITY_ID)
    assert state is not None


async def test_photo_url_updates_from_nearest_aircraft_info(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    holder.on_message("overhead", OVERHEAD_WITH_PHOTO)
    await hass.async_block_till_done()

    state = hass.states.get(_IMAGE_ENTITY_ID)
    assert state.state not in ("unknown", "unavailable")
    assert state.attributes["callsign"] == "QFA551"
    assert state.attributes["registration"] == "VH-VZR"


async def test_same_url_does_not_bump_last_updated(hass, setup_entry):
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    holder.on_message("overhead", OVERHEAD_WITH_PHOTO)
    await hass.async_block_till_done()
    first_updated = hass.states.get(_IMAGE_ENTITY_ID).state

    # A second frame with the exact same nearest-aircraft photo URL (e.g. a
    # routine ~5 s WS push) must not cache-bust image_last_updated.
    holder.on_message("overhead", copy.deepcopy(OVERHEAD_WITH_PHOTO))
    await hass.async_block_till_done()
    second = hass.states.get(_IMAGE_ENTITY_ID)

    assert second.state == first_updated
    assert second.attributes["callsign"] == "QFA551"


async def test_different_nearest_aircraft_bumps_last_updated(hass, setup_entry):
    """A frame whose nearest (index-0) aircraft has a different photo URL
    must update the image (and its cache-busting timestamp)."""
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    holder.on_message("overhead", OVERHEAD_WITH_PHOTO)
    await hass.async_block_till_done()
    first_updated = hass.states.get(_IMAGE_ENTITY_ID).state
    assert first_updated not in ("unknown", "unavailable")

    reordered = copy.deepcopy(OVERHEAD_WITH_PHOTO)
    # Swap order so aircraft[0] (the "nearest") is now JST812, and give it
    # its own, different photo URL.
    reordered["aircraft"][1]["info"] = {
        "manufacturer": "Airbus", "model": "A320-232", "owner": "Jetstar",
        "country": "Australia", "photo": "https://cdn.planespotters.net/photo/999999-jst812.jpg",
        "photo_thumb": None,
    }
    reordered["aircraft"] = [reordered["aircraft"][1], reordered["aircraft"][0]]
    holder.on_message("overhead", reordered)
    await hass.async_block_till_done()

    second = hass.states.get(_IMAGE_ENTITY_ID)
    assert second.state != first_updated
    assert second.attributes["callsign"] == "JST812"


async def test_no_photo_state_stays_unknown(hass, setup_entry):
    """OVERHEAD's nearest aircraft (QFA551) has info.photo == None and no
    photo_thumb, so the image entity must not report a URL or timestamp."""
    _entry, holder, _mock_stop = setup_entry
    holder.on_connection_change(True)

    holder.on_message("overhead", OVERHEAD)
    await hass.async_block_till_done()

    state = hass.states.get(_IMAGE_ENTITY_ID)
    assert state.state == "unknown"
    assert "callsign" not in state.attributes
