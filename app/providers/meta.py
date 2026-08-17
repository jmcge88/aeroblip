"""Aircraft metadata providers: routes, airframes and airlines.

Interchangeable implementations behind the same duck-typed interface
(fetch_route / fetch_aircraft / fetch_airline):

  AdsbdbMeta        free adsbdb.com - personal/self-hosted use only (its route
                    data is not licensed for redistribution or commercial
                    products). Returns planespotters.net photo URLs
                    (non-commercial terms).
  StandingDataMeta  app.providers.standing_data - product mode. VRS
                    standing-data (CC0) synced into a local SQLite DB; free,
                    commercially usable, no photos.
  LayeredMeta       primary provider with a fallback filling whole-result
                    misses and missing airline IATA codes (personal mode:
                    adsbdb over standing-data - adsbdb has no IATA for many
                    regionals, e.g. QantasLink, so their logos never resolved).
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
AIRLINE_URL = "https://api.adsbdb.com/v0/airline/{icao}"
AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{mode_s}"


class AdsbdbMeta:
    """Free adsbdb.com lookups (route by callsign, airframe by hex)."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def fetch_route(self, callsign: str) -> dict | None:
        """Route info for a callsign: {"origin", "origin_name", "destination",
        "destination_name", "airline", "airline_iata"} or None if unknown."""
        try:
            resp = await self._client.get(ROUTE_URL.format(callsign=callsign), timeout=10)
            if resp.status_code == 404:  # unknown callsign - not an error
                return None
            resp.raise_for_status()
            fr = (resp.json().get("response") or {}).get("flightroute") or {}
        except Exception as exc:
            log.warning("route lookup failed for %s: %s", callsign, exc)
            return None

        origin, dest = fr.get("origin") or {}, fr.get("destination") or {}
        if not origin or not dest:
            return None
        airline = fr.get("airline") or {}
        return {
            "origin": origin.get("iata_code") or origin.get("icao_code"),
            "origin_name": origin.get("municipality") or origin.get("name"),
            "destination": dest.get("iata_code") or dest.get("icao_code"),
            "destination_name": dest.get("municipality") or dest.get("name"),
            "airline": airline.get("name"),
            "airline_iata": airline.get("iata"),
        }

    async def fetch_aircraft(self, mode_s: str) -> dict | None:
        """Airframe details (manufacturer, model, owner, photo) by hex code."""
        try:
            resp = await self._client.get(AIRCRAFT_URL.format(mode_s=mode_s), timeout=10)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            ac = (resp.json().get("response") or {}).get("aircraft") or {}
        except Exception as exc:
            log.warning("aircraft lookup failed for %s: %s", mode_s, exc)
            return None
        if not ac:
            return None
        return {
            "manufacturer": ac.get("manufacturer"),
            "model": ac.get("type"),
            "owner": ac.get("registered_owner"),
            "country": ac.get("registered_owner_country_name"),
            "photo": ac.get("url_photo"),
            "photo_thumb": ac.get("url_photo_thumbnail"),
        }

    async def fetch_airline(self, icao_prefix: str) -> dict | None:
        """Airline by 3-letter ICAO callsign prefix (fallback when the full
        route is unknown)."""
        try:
            resp = await self._client.get(AIRLINE_URL.format(icao=icao_prefix), timeout=10)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            airlines = resp.json().get("response") or []
        except Exception as exc:
            log.warning("airline lookup failed for %s: %s", icao_prefix, exc)
            return None
        if not airlines:
            return None
        return {"airline": airlines[0].get("name"), "airline_iata": airlines[0].get("iata")}


class LayeredMeta:
    """Primary provider backed by a fallback for misses and missing fields."""

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    async def _fill_iata(self, result: dict, icao_prefix: str) -> dict:
        if result.get("airline") and not result.get("airline_iata"):
            fb = await self._fallback.fetch_airline(icao_prefix)
            if fb and fb.get("airline_iata"):
                result["airline_iata"] = fb["airline_iata"]
        return result

    async def fetch_route(self, callsign: str) -> dict | None:
        route = await self._primary.fetch_route(callsign)
        if route is None:
            return await self._fallback.fetch_route(callsign)
        return await self._fill_iata(route, callsign[:3])

    async def fetch_aircraft(self, mode_s: str) -> dict | None:
        aircraft = await self._primary.fetch_aircraft(mode_s)
        return aircraft if aircraft is not None \
            else await self._fallback.fetch_aircraft(mode_s)

    async def fetch_airline(self, icao_prefix: str) -> dict | None:
        airline = await self._primary.fetch_airline(icao_prefix)
        if airline is None:
            return await self._fallback.fetch_airline(icao_prefix)
        return await self._fill_iata(airline, icao_prefix)
