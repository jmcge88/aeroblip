"""Radar providers: readsb-style ADS-B aggregators with a common interface.

Each aggregator returns ADSBexchange-v2-style aircraft dicts but the point
query URL differs slightly per provider. Metadata (routes/airframes/airlines)
lives in app.providers.meta.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

# Point-query URL templates keyed by provider name.
#
# airplanes.live was removed on 16 Aug 2026: they closed the public API and now
# return 403 with "Please contact us at contact@airplanes.live" on every
# request. Keeping it in the pool only produced a steady drip of 403s at their
# end, so don't add it back without arranging access first.
PROVIDERS = {
    "adsblol": "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}",
    "adsbfi": "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{radius}",
}

# Minimum seconds between calls to the same aggregator, enforced globally
# across every location poller - N devices must share one upstream budget,
# not multiply it.
#
# adsb.lol publishes no fixed request-per-second figure: their API docs say
# rate limits are "dynamic based on the environment load", that 4xx means
# "you are doing something wrong", and that an API key obtained by feeding the
# network will eventually be required. So the contract we can actually honour
# is behavioural rather than numeric: space calls, and back off properly the
# moment they push back (see penalise / _penalty_until below) instead of
# retrying into a limit. If you run this at any scale, feed a receiver:
# https://adsb.lol/feed/
_MIN_SPACING = {"adsblol": 10.0, "adsbfi": 1.5}
_last_call: dict[str, float] = {}
_throttle_locks: dict[str, asyncio.Lock] = {}

# Cooldowns applied when an aggregator signals it has had enough. Escalates on
# repeat offences so a sustained block costs them one probe every 5 min, not a
# request every poll interval.
PENALTY_SECONDS = [60, 300, 900]
_penalty_until: dict[str, float] = {}
_penalty_level: dict[str, int] = {}


async def throttle(name: str) -> None:
    """Space calls to `name` globally. Public: any loop in the app that talks
    to an aggregator (e.g. the squawk watch in services.alerts) must call this
    so every consumer shares one upstream budget per provider."""
    lock = _throttle_locks.setdefault(name, asyncio.Lock())
    async with lock:
        wait = _last_call.get(name, 0.0) + _MIN_SPACING.get(name, 1.0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[name] = time.monotonic()


def penalised(name: str) -> bool:
    return time.monotonic() < _penalty_until.get(name, 0.0)


def penalise(name: str, retry_after: str | None = None) -> None:
    """Stand down from an aggregator that returned 429/403.

    Honours Retry-After when the server sends a plain seconds value; otherwise
    escalates through PENALTY_SECONDS.
    """
    level = _penalty_level.get(name, 0)
    cooldown = PENALTY_SECONDS[min(level, len(PENALTY_SECONDS) - 1)]
    if retry_after:
        try:  # seconds form only; HTTP-date is rare here and not worth parsing
            cooldown = max(cooldown, min(3600, int(retry_after.strip())))
        except ValueError:
            pass
    _penalty_level[name] = level + 1
    _penalty_until[name] = time.monotonic() + cooldown
    log.warning("radar provider %s rate-limited us - standing down %ds", name, cooldown)


def clear_penalty(name: str) -> None:
    if name in _penalty_level:
        _penalty_level.pop(name, None)
        _penalty_until.pop(name, None)

# After falling back, wait this long before re-trying the preferred provider.
# (adsb.lol rate-limits to roughly 1 request / 10 s, so fast pollers will
# stick to a fallback and probe the preferred source occasionally.)
PREFERRED_RETRY_SECONDS = 300

# When the active provider says "no aircraft", cross-check the others this
# often. A provider can be up but silently empty (e.g. adsb.lol returning
# 200 with zero aircraft globally), which stickiness alone would never catch.
EMPTY_CROSSCHECK_SECONDS = 60


class RadarProvider:
    """Fetches aircraft near a point, with sticky fallback across aggregators.

    `allowed` restricts the aggregator pool - product mode passes
    ["adsblol"] because adsb.fi's terms are non-commercial.
    """

    def __init__(self, client: httpx.AsyncClient, preferred: str = "adsblol",
                 allowed: list[str] | None = None):
        self._client = client
        self._providers = {name: url for name, url in PROVIDERS.items()
                           if allowed is None or name in allowed}
        if not self._providers:
            self._providers = {"adsblol": PROVIDERS["adsblol"]}
        self._preferred = preferred if preferred in self._providers else next(iter(self._providers))
        self.active = self._preferred
        self._preferred_last_try = 0.0
        self._empty_crosscheck_last = 0.0

    async def _get_point(self, name: str, lat: float, lon: float, radius_nm: float) -> list[dict]:
        await throttle(name)
        url = self._providers[name].format(lat=f"{lat:.6f}", lon=f"{lon:.6f}",
                                           radius=f"{radius_nm:g}")
        resp = await self._client.get(url, timeout=10)
        # 429 = slow down, 403 = go away (how airplanes.live closed their API).
        # Either way, stop asking for a while instead of retrying every poll.
        if resp.status_code in (403, 429):
            penalise(name, resp.headers.get("Retry-After"))
            resp.raise_for_status()
        resp.raise_for_status()
        clear_penalty(name)
        return resp.json().get("ac") or []

    async def fetch_point(self, lat: float, lon: float, radius_nm: float) -> list[dict]:
        """Return raw aircraft dicts within radius_nm of (lat, lon).

        Sticky: keeps using whichever provider last worked, probing the
        preferred provider again after a cooldown. An empty (but successful)
        response is periodically cross-checked against the other providers in
        case the active one has gone quietly blind.
        """
        now = time.monotonic()
        order = [self.active] + [p for p in self._providers if p != self.active]
        if (self.active != self._preferred
                and now - self._preferred_last_try > PREFERRED_RETRY_SECONDS):
            order.remove(self._preferred)
            order.insert(0, self._preferred)

        last_error: Exception | None = None
        empty_from: str | None = None
        for name in order:
            if penalised(name):
                continue  # still standing down from a 429/403
            if name == self._preferred:
                self._preferred_last_try = now
            try:
                aircraft = await self._get_point(name, lat, lon, radius_nm)
            except Exception as exc:  # network / HTTP / JSON errors -> try next
                last_error = exc
                log.warning("radar provider %s failed: %s", name, exc)
                continue
            if not aircraft:
                if empty_from is None:
                    empty_from = name
                if now - self._empty_crosscheck_last > EMPTY_CROSSCHECK_SECONDS:
                    self._empty_crosscheck_last = now
                    continue  # verify the empty sky with the next provider
                break  # recently cross-checked; trust the empty result
            if name != self.active:
                log.info("radar provider switched to %s", name)
            self.active = name
            return aircraft

        if empty_from is not None:  # all reachable providers agree: clear skies
            if empty_from != self.active:
                log.info("radar provider switched to %s", empty_from)
            self.active = empty_from
            return []
        if last_error is None:  # nothing was even tried: every source is cooling off
            log.warning("all radar providers are standing down after rate limits")
        else:
            log.error("all radar providers failed: %s", last_error)
        return []
