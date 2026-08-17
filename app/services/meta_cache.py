"""Process-wide, disk-backed cache in front of a metadata provider.

Routes and airframes are properties of a callsign or a hex code, not of a
location - so every location poller in the fleet should share one cache, and it
should outlive both idle-reaping and server restarts. Without this each poller
kept private caches that died with it, and every restart re-bought the same
lookups from scratch. On a paid provider that is the difference between a
few hundred calls a day and several thousand.

Wraps any provider with the fetch_route / fetch_aircraft / fetch_airline
interface (see app.providers.meta) and presents the same one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Routes are stable within a day (QFA551 flies the same sectors); airframes are
# effectively immutable. Misses expire sooner - a callsign unknown this morning
# may be in the upstream database this evening.
TTL = {
    "route": (18 * 3600, 6 * 3600),                    # (hit, miss)
    "aircraft": (30 * 24 * 3600, 7 * 24 * 3600),
    "airline": (30 * 24 * 3600, 7 * 24 * 3600),
}
MAX_ENTRIES = {"route": 5000, "aircraft": 20000, "airline": 2000}
FLUSH_SECONDS = 60  # at most one disk write a minute, however busy the fleet
# Bump when a provider's payload shape changes - entries cached for up to 30
# days would otherwise keep serving the old shape (e.g. missing airline_iata)
SCHEMA = 3


class CachedMeta:
    def __init__(self, inner, path: str | Path):
        self._inner = inner
        self._path = Path(path)
        self._cache: dict[str, dict[str, dict]] = {k: {} for k in TTL}
        self._inflight: dict[tuple[str, str], asyncio.Future] = {}
        self._dirty = False
        self._last_flush = time.time()
        self.hits = 0
        self.misses = 0
        self._load()

    # ---- public provider interface ---------------------------------------

    async def fetch_route(self, callsign: str) -> dict | None:
        return await self._get("route", callsign, self._inner.fetch_route)

    async def fetch_aircraft(self, mode_s: str) -> dict | None:
        return await self._get("aircraft", mode_s, self._inner.fetch_aircraft)

    async def fetch_airline(self, icao_prefix: str) -> dict | None:
        return await self._get("airline", icao_prefix, self._inner.fetch_airline)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else None,
            "entries": {kind: len(c) for kind, c in self._cache.items()},
        }

    # ---- internals --------------------------------------------------------

    async def _get(self, kind: str, key: str, fetch):
        key = (key or "").strip().upper()
        if not key:
            return None
        entry = self._cache[kind].get(key)
        if entry is not None and not self._expired(kind, entry):
            self.hits += 1
            return entry["v"]

        # One upstream call per key even when several pollers want it at once.
        # shield() so a cancelled caller (a reaped poller) can't kill the
        # lookup another caller is still waiting on.
        inflight_key = (kind, key)
        task = self._inflight.get(inflight_key)
        if task is None:
            task = asyncio.ensure_future(self._fetch_and_store(kind, key, fetch))
            self._inflight[inflight_key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(inflight_key, None))
            self.misses += 1  # only this caller costs an upstream lookup
        else:
            self.hits += 1  # coalesced onto one already in flight
        return await asyncio.shield(task)

    async def _fetch_and_store(self, kind: str, key: str, fetch):
        try:
            value = await fetch(key)
        except Exception:  # providers swallow their own errors; belt and braces
            log.exception("%s lookup failed for %s", kind, key)
            return None
        self._cache[kind][key] = {"v": value, "t": time.time()}
        self._dirty = True
        self._maybe_flush()
        return value

    @staticmethod
    def _expired(kind: str, entry: dict) -> bool:
        hit_ttl, miss_ttl = TTL[kind]
        ttl = hit_ttl if entry.get("v") else miss_ttl
        return time.time() - entry.get("t", 0) > ttl

    def _prune(self) -> None:
        for kind, cache in self._cache.items():
            for key in [k for k, e in cache.items() if self._expired(kind, e)]:
                del cache[key]
            excess = len(cache) - MAX_ENTRIES[kind]
            if excess > 0:  # drop the oldest rather than clearing the lot
                for key, _ in sorted(cache.items(), key=lambda kv: kv[1]["t"])[:excess]:
                    del cache[key]

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            if data.get("_schema") != SCHEMA:
                log.info("metadata cache schema changed, starting empty")
                return
            for kind in self._cache:
                loaded = data.get(kind)
                if isinstance(loaded, dict):
                    self._cache[kind] = loaded
            self._prune()
            log.info("metadata cache loaded: %s", self.stats()["entries"])
        except Exception:
            log.warning("metadata cache unreadable, starting empty", exc_info=True)
            self._cache = {k: {} for k in TTL}

    def _maybe_flush(self) -> None:
        if self._dirty and time.time() - self._last_flush >= FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        """Write the cache out atomically; never fatal if it fails."""
        self._prune()
        self._last_flush = time.time()
        self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"_schema": SCHEMA, **self._cache}))
            os.replace(tmp, self._path)
        except Exception:
            log.warning("could not persist metadata cache", exc_info=True)
