"""Cached airport arrivals/departures board with quiet-hours refresh policy."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from app import config
from app.providers.board import BoardProvider

log = logging.getLogger(__name__)


def in_quiet_hours(hour: int) -> bool:
    start, end = config.BOARD_QUIET_START, config.BOARD_QUIET_END
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


class BoardCache:
    def __init__(self, provider: BoardProvider, airport: str | None = None):
        self._provider = provider
        self.last_used = time.monotonic()  # idle-reaping (see services.hub)
        if airport:  # device-supplied code; env config names the default airport
            code = airport.strip().upper()
            airport_info = {"icao": code if len(code) == 4 else "",
                            "iata": code if len(code) < 4 else "", "name": code}
        else:
            airport_info = {"icao": config.AIRPORT_ICAO, "iata": config.AIRPORT_IATA,
                            "name": config.AIRPORT_NAME}
        self.snapshot: dict = {
            "arrivals": [], "departures": [], "updated": None,
            "airport": airport_info,
            "mock": provider.is_mock,
            "unavailable": provider.is_unavailable,
        }

    def touch(self) -> None:
        self.last_used = time.monotonic()

    async def run(self) -> None:
        while True:
            if in_quiet_hours(datetime.now().hour) and self.snapshot["updated"]:
                await asyncio.sleep(300)
                continue
            try:
                board = await self._provider.fetch_board()
                self.snapshot = {
                    **self.snapshot,
                    "arrivals": board.get("arrivals", []),
                    "departures": board.get("departures", []),
                    "updated": int(time.time()),
                    "mock": bool(board.get("mock")),
                    "unavailable": bool(board.get("unavailable")),
                }
            except Exception:
                log.exception("board refresh failed; keeping previous data")
            await asyncio.sleep(config.BOARD_REFRESH_MINUTES * 60)
