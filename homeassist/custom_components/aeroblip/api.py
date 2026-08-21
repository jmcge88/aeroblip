"""Async client for the Aeroblip self-hosted flight-tracking server.

Talks to the two REST endpoints used for setup/validation and to the
`/ws` WebSocket stream that pushes overhead/board/alert snapshots for
the lifetime of the config entry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

import aiohttp
from yarl import URL

from .const import WS_CLOSE_AUTH

_LOGGER = logging.getLogger(__name__)

_HEALTH_PATH = "/api/health"
_CONFIG_PATH = "/api/config"
_WS_PATH = "/ws"

_TOKEN_HEADER = "X-Device-Token"

_CONNECT_TIMEOUT = 10  # seconds
_WS_HEARTBEAT = 30  # seconds - lets aiohttp detect a dead link and close the ws

_BACKOFF_INITIAL = 5  # seconds
_BACKOFF_MAX = 300  # seconds
_BACKOFF_JITTER = 0.2  # +/-20%


class AeroblipError(Exception):
    """Base error for all Aeroblip client failures."""


class AeroblipConnectionError(AeroblipError):
    """Raised when the server can't be reached or responds unexpectedly."""


class AeroblipAuthError(AeroblipError):
    """Raised when the device token is missing or rejected by the server."""


class AeroblipClient:
    """Thin async client for the Aeroblip REST + WebSocket API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        *,
        device_token: str | None = None,
        latitude: float,
        longitude: float,
        radius_nm: float,
        area_nm: float,
        airport: str,
    ) -> None:
        # Session is injected and owned by the caller (HA's shared session) -
        # this client never creates or closes it.
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._device_token = device_token
        self._latitude = latitude
        self._longitude = longitude
        self._radius_nm = radius_nm
        self._area_nm = area_nm
        self._airport = airport
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    def _headers(self) -> dict[str, str]:
        # Never log this - it's the only credential the integration holds.
        if self._device_token:
            return {_TOKEN_HEADER: self._device_token}
        return {}

    async def _get_json(self, path: str) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=_CONNECT_TIMEOUT)
        try:
            async with self._session.get(
                url, headers=self._headers(), timeout=timeout
            ) as resp:
                if resp.status in (401, 403):
                    raise AeroblipAuthError(
                        f"Aeroblip rejected the device token ({resp.status})"
                    )
                if resp.status != 200:
                    raise AeroblipConnectionError(
                        f"Unexpected status {resp.status} from {path}"
                    )
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as err:
                    raise AeroblipConnectionError(
                        f"Non-JSON response from {path}"
                    ) from err
        except AeroblipError:
            raise
        except asyncio.TimeoutError as err:
            raise AeroblipConnectionError(f"Timed out contacting {path}") from err
        except aiohttp.ClientError as err:
            raise AeroblipConnectionError(f"Connection error contacting {path}") from err

    async def async_validate(self) -> dict[str, Any]:
        """Validate connectivity and auth against /api/health."""
        return await self._get_json(_HEALTH_PATH)

    async def async_get_server_config(self) -> dict[str, Any]:
        """Fetch server-side location/airport config from /api/config."""
        return await self._get_json(_CONFIG_PATH)

    def _ws_url(self) -> URL:
        # str-replace the scheme rather than yarl's with_scheme() so any path
        # segment already present in base_url (reverse proxies etc.) survives.
        ws_base = self._base_url.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        return URL(f"{ws_base}{_WS_PATH}").with_query(
            lat=str(self._latitude),
            lon=str(self._longitude),
            radius=str(self._radius_nm),
            area=str(self._area_nm),
            airport=self._airport,
        )

    async def async_run(
        self,
        on_message: Callable[[str, dict[str, Any]], None],
        on_connection_change: Callable[[bool], None],
    ) -> None:
        """Connect to the Aeroblip WebSocket and dispatch frames until cancelled.

        Runs forever, reconnecting with backoff, until the task is cancelled
        or the server rejects the device token (WS_CLOSE_AUTH), which ends
        the loop by raising so the caller can trigger a reauth flow.
        """
        backoff = _BACKOFF_INITIAL
        while True:
            auth_failed: AeroblipAuthError | None = None
            try:
                # No timeout kwarg: aiohttp deprecated the float form (it means
                # close-timeout, not connect); the heartbeat detects dead links.
                async with self._session.ws_connect(
                    self._ws_url(),
                    headers=self._headers(),
                    heartbeat=_WS_HEARTBEAT,
                ) as ws:
                    self._ws = ws
                    on_connection_change(True)
                    backoff = _BACKOFF_INITIAL  # reset once a connection succeeds
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except json.JSONDecodeError:
                            _LOGGER.debug("Ignoring malformed Aeroblip WS frame")
                            continue
                        if (
                            not isinstance(payload, dict)
                            or "type" not in payload
                            or "data" not in payload
                        ):
                            _LOGGER.debug(
                                "Ignoring Aeroblip WS frame missing type/data"
                            )
                            continue
                        on_message(payload["type"], payload["data"])

                    if ws.close_code == WS_CLOSE_AUTH:
                        auth_failed = AeroblipAuthError(
                            "Aeroblip closed the WebSocket: bad device token"
                        )
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                # Some deployments reject the handshake outright instead of
                # accepting then closing with WS_CLOSE_AUTH.
                if err.status in (401, 403):
                    auth_failed = AeroblipAuthError(
                        f"Aeroblip rejected the WS handshake ({err.status})"
                    )
                else:
                    _LOGGER.debug("Aeroblip WS handshake failed: %s", err)
            except aiohttp.ClientError as err:
                _LOGGER.debug("Aeroblip WS connection error: %s", err)
            finally:
                self._ws = None
                on_connection_change(False)

            if auth_failed is not None:
                # A bad token won't fix itself with retries - end the loop so
                # the coordinator can start reauth instead of retrying forever.
                raise auth_failed

            sleep_for = backoff * random.uniform(
                1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER
            )
            _LOGGER.debug("Reconnecting to Aeroblip in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def async_stop(self) -> None:
        """Close any live WebSocket connection so async_run's task can end cleanly."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
