"""AeroDataBox FIDS provider for the airport arrivals/departures board.

Works with either marketplace:
  - RapidAPI:   https://rapid.aerodatabox.com/
  - API.Market: https://apimarket.aerodatabox.com/

In demo mode fabricated board data is returned. Without a key (and not in
demo mode) the board reports itself unavailable - data is never made up.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

import httpx

from app.providers.aerodatabox import request_parts

log = logging.getLogger(__name__)


class BoardProvider:
    def __init__(self, client: httpx.AsyncClient, api_key: str, market: str, airport: str,
                 demo: bool = False):
        self._client = client
        self._api_key = api_key
        self._market = market
        # 4 chars = ICAO (YBBN), 2-3 chars = IATA (BNE) - AeroDataBox takes both
        self._airport = (airport or "").strip().upper()
        self._demo = demo

    @property
    def is_mock(self) -> bool:
        return self._demo

    @property
    def is_unavailable(self) -> bool:
        return not self._demo and not self._api_key

    def _request_parts(self, from_local: str, to_local: str) -> tuple[str, dict]:
        kind = "icao" if len(self._airport) == 4 else "iata"
        path = (
            f"/flights/airports/{kind}/{self._airport}/{from_local}/{to_local}"
            "?withLeg=true&direction=Both&withCancelled=true"
            "&withCodeshared=false&withCargo=true&withPrivate=false&withLocation=false"
        )
        return request_parts(self._market, self._api_key, path)

    async def fetch_board(self) -> dict:
        """Return {"arrivals": [...], "departures": [...]} normalized rows."""
        if self._demo:
            return self._mock_board()
        if self.is_unavailable:
            return {"arrivals": [], "departures": [], "unavailable": True}

        now = datetime.now()
        from_local = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        to_local = (now + timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M")
        url, headers = self._request_parts(from_local, to_local)
        resp = await self._client.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return {
            "arrivals": [self._normalize(f, "arrival") for f in data.get("arrivals", [])],
            "departures": [self._normalize(f, "departure") for f in data.get("departures", [])],
        }

    @staticmethod
    def _normalize(flight: dict, direction: str) -> dict:
        # Each flight carries "departure" and "arrival" objects: our airport's
        # side has the times/gate, the opposite side names the other airport.
        movement = flight.get(direction) or {}
        other = flight.get("arrival" if direction == "departure" else "departure") or {}
        sched = (movement.get("scheduledTime") or {}).get("local")
        revised = (movement.get("revisedTime") or {}).get("local")
        other_airport = other.get("airport") or {}
        return {
            "flight": (flight.get("number") or "").replace(" ", ""),
            "airline": (flight.get("airline") or {}).get("name") or "",
            "city": other_airport.get("name") or other_airport.get("iata") or "",
            "code": other_airport.get("iata") or other_airport.get("icao") or "",
            "scheduled": sched,
            "estimated": revised,
            "terminal": movement.get("terminal"),
            "gate": movement.get("gate"),
            "status": flight.get("status") or "",
            "aircraft": (flight.get("aircraft") or {}).get("model") or "",
            "direction": direction,
        }

    def _mock_board(self) -> dict:
        """Plausible fake BNE board for demo mode."""
        airlines = [
            ("QF", "Qantas"), ("VA", "Virgin Australia"), ("JQ", "Jetstar"),
            ("NZ", "Air New Zealand"), ("SQ", "Singapore Airlines"), ("EK", "Emirates"),
        ]
        cities = [
            ("Sydney", "SYD"), ("Melbourne", "MEL"), ("Cairns", "CNS"),
            ("Auckland", "AKL"), ("Singapore", "SIN"), ("Perth", "PER"),
            ("Townsville", "TSV"), ("Darwin", "DRW"), ("Los Angeles", "LAX"),
        ]
        statuses = ["Expected", "Expected", "Expected", "Delayed", "Boarding", "CheckIn"]
        rng = random.Random(42)
        now = datetime.now().replace(second=0, microsecond=0)

        def rows(direction: str) -> list[dict]:
            out = []
            for i in range(12):
                code, airline = rng.choice(airlines)
                city, iata = rng.choice(cities)
                sched = now + timedelta(minutes=18 * i + rng.randint(0, 12))
                status = rng.choice(statuses)
                delayed = status == "Delayed"
                out.append({
                    "flight": f"{code}{rng.randint(100, 999)}",
                    "airline": airline,
                    "city": city,
                    "code": iata,
                    "scheduled": sched.isoformat(),
                    "estimated": (sched + timedelta(minutes=25)).isoformat() if delayed else None,
                    "terminal": rng.choice(["D", "I"]),
                    "gate": f"{rng.choice('ABC')}{rng.randint(1, 24)}",
                    "status": status,
                    "aircraft": rng.choice(["Boeing 737-800", "Airbus A320", "Boeing 787-9"]),
                    "direction": direction,
                })
            return out

        return {"arrivals": rows("arrival"), "departures": rows("departure"), "mock": True}
