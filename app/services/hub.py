"""On-demand per-location pollers and per-airport boards.

Every sold device carries its own location/airport, so the server runs one
OverheadPoller per distinct location and one BoardCache per distinct airport.
Entries are created on first request, kept alive while used, and reaped after
sitting idle - so upstream load tracks *active* locations, not sold units.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app import config
from app.providers.board import BoardProvider
from app.providers.radar import RadarProvider
from app.services.board_cache import BoardCache
from app.services.poller import OverheadPoller

log = logging.getLogger(__name__)

POLLER_IDLE_SECONDS = 300
BOARD_IDLE_SECONDS = 3600
REAP_EVERY_SECONDS = 60


class TooManyLocations(Exception):
    pass


class LocationHub:
    def __init__(self, client: httpx.AsyncClient, meta):
        self._client = client
        self._meta = meta
        self._pollers: dict[tuple, dict] = {}
        self._boards: dict[str, dict] = {}

    def _make_radar(self) -> RadarProvider:
        if config.PRODUCT_MODE:
            return RadarProvider(self._client, preferred="adsblol", allowed=["adsblol"])
        return RadarProvider(self._client, preferred=config.ADSB_PROVIDER)

    def _evict_idle_poller(self) -> bool:
        """Drop the least-recently-used poller with no live websocket client.

        Refusing new locations at the cap would let anyone walk MAX_LOCATIONS
        arbitrary coordinates and lock real devices out until the idle timeout.
        Evicting instead means an abusive caller only churns its own entries;
        anything actively streaming to a client is never evicted.
        """
        idle = [(k, e) for k, e in self._pollers.items() if not e["poller"].busy]
        if not idle:
            return False
        key, entry = min(idle, key=lambda kv: kv[1]["poller"].last_used)
        entry["task"].cancel()
        del self._pollers[key]
        log.info("location poller evicted at cap: %s (%d active)", key, len(self._pollers))
        return True

    def _evict_idle_board(self) -> bool:
        """Drop the least-recently-used board no live poller depends on."""
        in_use = {(e["board"] or "").strip().upper() for e in self._pollers.values()}
        idle = [(c, e) for c, e in self._boards.items() if c not in in_use]
        if not idle:
            return False
        code, entry = min(idle, key=lambda kv: kv[1]["cache"].last_used)
        entry["task"].cancel()
        del self._boards[code]
        log.info("board cache evicted at cap: %s (%d active)", code, len(self._boards))
        return True

    async def poller_for(self, lat: float, lon: float, overhead_nm: float,
                         area_nm: float, airport: str | None) -> OverheadPoller:
        key = (round(lat, 4), round(lon, 4), round(overhead_nm, 1), round(area_nm, 0),
               airport or "")
        entry = self._pollers.get(key)
        if entry is None:
            if len(self._pollers) >= config.MAX_LOCATIONS and not self._evict_idle_poller():
                raise TooManyLocations()
            board = self.board_for(airport) if airport else None
            poller = OverheadPoller(
                self._make_radar(), self._meta, board_cache=board,
                lat=lat, lon=lon, overhead_nm=overhead_nm, area_nm=area_nm,
                airport_iata=airport)
            entry = {"poller": poller, "board": airport,
                     "task": asyncio.create_task(poller.run())}
            self._pollers[key] = entry
            log.info("location poller started: %s (%d active)", key, len(self._pollers))
            try:  # first request returns real data instead of an empty snapshot
                await poller.poll_now()
            except Exception:
                log.exception("initial poll failed for %s", key)
        entry["poller"].touch()
        return entry["poller"]

    def board_for(self, airport: str) -> BoardCache:
        code = airport.strip().upper()
        entry = self._boards.get(code)
        if entry is None:
            # Every new code costs AeroDataBox calls for as long as it lives, so
            # the cap is a spend limit, not just a memory one.
            if len(self._boards) >= config.MAX_AIRPORTS and not self._evict_idle_board():
                raise TooManyLocations()
            provider = BoardProvider(self._client, config.AERODATABOX_API_KEY,
                                     config.AERODATABOX_MARKET, code, demo=config.DEMO_MODE)
            cache = BoardCache(provider, airport=code)
            entry = {"cache": cache, "task": asyncio.create_task(cache.run())}
            self._boards[code] = entry
            log.info("board cache started: %s (%d active)", code, len(self._boards))
        entry["cache"].touch()
        return entry["cache"]

    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(REAP_EVERY_SECONDS)
            now = time.monotonic()
            for key, entry in list(self._pollers.items()):
                p = entry["poller"]
                if p.busy or now - p.last_used < POLLER_IDLE_SECONDS:
                    if entry["board"]:  # a live poller keeps its airport board alive
                        board = self._boards.get(entry["board"].strip().upper())
                        if board:
                            board["cache"].touch()
                    continue
                entry["task"].cancel()
                del self._pollers[key]
                log.info("location poller reaped: %s (%d active)", key, len(self._pollers))
            for code, entry in list(self._boards.items()):
                if now - entry["cache"].last_used > BOARD_IDLE_SECONDS:
                    entry["task"].cancel()
                    del self._boards[code]
                    log.info("board cache reaped: %s (%d active)", code, len(self._boards))
