"""Unit tests for AeroblipClient - fully offline.

URL building and header construction are pure/sync and tested directly with
no hass involved at all. async_validate's status-code -> exception mapping
is exercised through ``async_get_clientsession(hass)`` + ``aioclient_mock``:
aioclient_mock only intercepts sessions obtained via that helper (it patches
the helper itself, not ``aiohttp.ClientSession``), so a session must come
from ``async_get_clientsession`` for the mock to take effect - a raw
``aiohttp.ClientSession()`` would fall through to real DNS resolution, which
the sandbox has no sockets for.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.aeroblip.api import (
    AeroblipAuthError,
    AeroblipClient,
    AeroblipConnectionError,
)


def _client(**overrides) -> AeroblipClient:
    kwargs = dict(
        session=None,
        base_url="https://example.com:8000",
        device_token=None,
        latitude=-27.3842,
        longitude=153.1175,
        radius_nm=5.0,
        area_nm=60.0,
        airport="BNE",
    )
    kwargs.update(overrides)
    session = kwargs.pop("session")
    base_url = kwargs.pop("base_url")
    return AeroblipClient(session, base_url, **kwargs)


def test_ws_url_https_becomes_wss():
    client = _client(base_url="https://example.com:8000")
    url = client._ws_url()
    assert str(url).startswith("wss://example.com:8000/ws?")


def test_ws_url_http_becomes_ws():
    client = _client(base_url="http://192.168.1.10:8000")
    url = client._ws_url()
    assert str(url).startswith("ws://192.168.1.10:8000/ws?")


def test_ws_url_query_params_present():
    client = _client(
        base_url="http://example.com",
        latitude=-27.3842,
        longitude=153.1175,
        radius_nm=5.0,
        area_nm=60.0,
        airport="BNE",
    )
    url = client._ws_url()
    assert url.query["lat"] == "-27.3842"
    assert url.query["lon"] == "153.1175"
    assert url.query["radius"] == "5.0"
    assert url.query["area"] == "60.0"
    assert url.query["airport"] == "BNE"


def test_ws_url_preserves_reverse_proxy_path():
    client = _client(base_url="https://example.com/aeroblip")
    url = client._ws_url()
    assert str(url).startswith("wss://example.com/aeroblip/ws?")


def test_headers_with_token():
    client = _client(device_token="tok123")
    assert client._headers() == {"X-Device-Token": "tok123"}


def test_headers_without_token():
    client = _client(device_token=None)
    assert client._headers() == {}


async def test_async_validate_success(hass, aioclient_mock):
    aioclient_mock.get(
        "http://example.com/api/health",
        json={"ok": True, "product": False, "meta_cache": {}},
    )
    client = _client(session=async_get_clientsession(hass), base_url="http://example.com")
    result = await client.async_validate()
    assert result["ok"] is True


async def test_async_validate_auth_error_on_401(hass, aioclient_mock):
    aioclient_mock.get("http://example.com/api/health", status=401)
    client = _client(session=async_get_clientsession(hass), base_url="http://example.com")
    with pytest.raises(AeroblipAuthError):
        await client.async_validate()


async def test_async_validate_auth_error_on_403(hass, aioclient_mock):
    aioclient_mock.get("http://example.com/api/health", status=403)
    client = _client(session=async_get_clientsession(hass), base_url="http://example.com")
    with pytest.raises(AeroblipAuthError):
        await client.async_validate()


async def test_async_validate_connection_error_on_500(hass, aioclient_mock):
    aioclient_mock.get("http://example.com/api/health", status=500)
    client = _client(session=async_get_clientsession(hass), base_url="http://example.com")
    with pytest.raises(AeroblipConnectionError):
        await client.async_validate()


async def test_async_validate_connection_error_on_non_json(hass, aioclient_mock):
    aioclient_mock.get(
        "http://example.com/api/health",
        text="not json",
        headers={"content-type": "text/plain"},
    )
    client = _client(session=async_get_clientsession(hass), base_url="http://example.com")
    with pytest.raises(AeroblipConnectionError):
        await client.async_validate()
