"""Offline aircraft metadata from VRS standing-data (CC0-1.0).

Downloads the vradarserver/standing-data repository (community-maintained CSV
dumps of routes, airlines, airports and airframes; public domain) into a local
SQLite database, refreshed daily, and serves the same three-method interface
as the other meta providers (fetch_route / fetch_aircraft / fetch_airline).

Lookups cost nothing, so product mode needs no paid metadata plan -
AeroDataBox is kept for the live airport board only. Airframe data here is
sparse (user submissions since 2022); the primary airframe source remains the
r/t/desc fields already present in the adsb.lol position feed.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "https://github.com/vradarserver/standing-data/archive/refs/heads/main.tar.gz"

_SCHEMA = """
CREATE TABLE routes   (callsign TEXT PRIMARY KEY, airline_code TEXT, airport_codes TEXT);
CREATE TABLE airports (code TEXT PRIMARY KEY, name TEXT, icao TEXT, iata TEXT,
                       location TEXT, country TEXT);
CREATE TABLE airlines (code TEXT, name TEXT, icao TEXT, iata TEXT);
CREATE INDEX airlines_code ON airlines(code);
CREATE INDEX airlines_icao ON airlines(icao);
CREATE INDEX airlines_iata ON airlines(iata);
CREATE TABLE aircraft (icao TEXT PRIMARY KEY, registration TEXT, model_icao TEXT,
                       manufacturer TEXT, model TEXT, operator TEXT, airline_code TEXT);
CREATE TABLE built    (key TEXT PRIMARY KEY, value TEXT);
"""

# Callsign normalisation, matching the SDM site's scheme (see the routes
# schema README): split into code + number, strip leading zeros from the
# number, re-add one zero if nothing but letters remains, then validate.
_CALLSIGN_RE = re.compile(r"^([A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])(\d[A-Z0-9]*)$")
_NUMBER_RE = re.compile(r"^(?:\d{1,4}|\d{1,3}[A-Z]|\d{1,2}[A-Z]{2})$")


def normalise_callsign(callsign: str) -> tuple[str, str] | None:
    m = _CALLSIGN_RE.match(callsign)
    if not m:
        return None
    code, number = m.group(1), m.group(2)
    number = number.lstrip("0")
    if not number or number.isalpha():
        number = "0" + number
    if not _NUMBER_RE.match(number):
        return None
    return code, number


# ---- database build (runs in a worker thread) ------------------------------

def _insert_rows(conn: sqlite3.Connection, table: str, fields: list[str],
                 reader: csv.DictReader, skip=None) -> int:
    rows = [tuple((row.get(f) or "").strip() for f in fields)
            for row in reader if skip is None or not skip(row)]
    if rows:
        marks = ",".join("?" * len(fields))
        conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", rows)
    return len(rows)


_DATASETS = {
    "routes": ("routes", ["Callsign", "AirlineCode", "AirportCodes"], None),
    "airports": ("airports",
                 ["Code", "Name", "ICAO", "IATA", "Location", "CountryISO2"], None),
    "airlines": ("airlines", ["Code", "Name", "ICAO", "IATA"], None),
    # Fake model types (-GND ground vehicles, -TWR fixed installations) are
    # not aircraft; leave them out.
    "aircraft": ("aircraft",
                 ["ICAO", "Registration", "ModelICAO", "Manufacturer", "Model",
                  "Operator", "AirlineCode"],
                 lambda row: (row.get("ModelICAO") or "").startswith("-")),
}


def _build(tar_path: Path, db_path: Path) -> dict[str, int]:
    """Build a fresh SQLite database from the repo tarball."""
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(_SCHEMA)
        counts = dict.fromkeys(_DATASETS, 0)
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar:
                # e.g. standing-data-main/routes/schema-01/Q/QFA-all.csv
                parts = member.name.split("/")
                if (not member.isfile() or not member.name.endswith(".csv")
                        or len(parts) < 4 or parts[2] != "schema-01"
                        or parts[1] not in _DATASETS):
                    continue
                table, fields, skip = _DATASETS[parts[1]]
                reader = csv.DictReader(
                    io.TextIOWrapper(tar.extractfile(member), encoding="utf-8-sig"))
                counts[parts[1]] += _insert_rows(conn, table, fields, reader, skip)
        conn.execute("INSERT INTO built VALUES ('built_at', ?)", (str(time.time()),))
        conn.commit()
        return counts
    finally:
        conn.close()


# ---- provider ---------------------------------------------------------------

class StandingDataMeta:
    """Route/airframe/airline lookups from a local standing-data SQLite DB."""

    def __init__(self, client: httpx.AsyncClient, db_path: str | Path,
                 url: str = DEFAULT_URL, refresh_hours: float = 24):
        self._client = client
        self._db_path = Path(db_path)
        self._url = url
        self._refresh_seconds = refresh_hours * 3600
        self._conn: sqlite3.Connection | None = None
        if self._db_path.exists():
            self._connect()

    @property
    def ready(self) -> bool:
        return self._conn is not None

    def _connect(self) -> None:
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def _one(self, sql: str, *params) -> sqlite3.Row | None:
        if self._conn is None:
            return None
        try:
            return self._conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            log.warning("standing-data query failed: %s", exc)
            return None

    def _built_at(self) -> float:
        row = self._one("SELECT value FROM built WHERE key='built_at'")
        return float(row["value"]) if row else 0.0

    # ---- sync ---------------------------------------------------------------

    async def sync(self) -> None:
        """Download the tarball, rebuild the database, hot-swap the connection."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tar_name = tempfile.mkstemp(suffix=".tar.gz", dir=self._db_path.parent)
        tar_path = Path(tar_name)
        try:
            log.info("standing-data: downloading %s", self._url)
            with os.fdopen(fd, "wb") as out:
                async with self._client.stream(
                        "GET", self._url, follow_redirects=True,
                        timeout=httpx.Timeout(600, connect=30)) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes():
                        out.write(chunk)
            new_db = self._db_path.with_suffix(".db.new")
            counts = await asyncio.to_thread(_build, tar_path, new_db)
            # Close before replacing: Windows cannot swap an open file. The
            # sub-ms gap can only cost a lookup a transient None.
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            os.replace(new_db, self._db_path)
            self._connect()
            log.info("standing-data: database rebuilt (%s)",
                     ", ".join(f"{k}={v}" for k, v in counts.items()))
        finally:
            tar_path.unlink(missing_ok=True)

    async def run(self) -> None:
        """Refresh the database whenever it goes stale (default: daily)."""
        while True:
            if time.time() - self._built_at() > self._refresh_seconds:
                try:
                    await self.sync()
                except Exception:
                    log.exception("standing-data sync failed")
                    await asyncio.sleep(900)
                    continue
            await asyncio.sleep(3600)

    # ---- provider interface -------------------------------------------------

    async def fetch_route(self, callsign: str) -> dict | None:
        """Route info for a callsign, same shape as AdsbdbMeta.fetch_route."""
        norm = normalise_callsign(callsign)
        if norm is None:
            return None
        code, number = norm
        row = self._one("SELECT airline_code, airport_codes FROM routes"
                        " WHERE callsign=?", code + number)
        if row is None and len(code) == 2:
            # Pilot entered the IATA form; the DB keys on the ICAO code.
            icao = self._one("SELECT icao FROM airlines WHERE iata=? AND icao!=''", code)
            if icao:
                row = self._one("SELECT airline_code, airport_codes FROM routes"
                                " WHERE callsign=?", icao["icao"] + number)
        if row is None:
            return None
        codes = [c for c in row["airport_codes"].split("-") if c]
        if len(codes) < 2:
            return None
        origin, dest = self._airport(codes[0]), self._airport(codes[-1])
        airline = self._one("SELECT name, iata FROM airlines WHERE code=?",
                            row["airline_code"])
        return {
            "origin": origin[0], "origin_name": origin[1],
            "destination": dest[0], "destination_name": dest[1],
            "airline": airline["name"] if airline else None,
            "airline_iata": (airline["iata"] or None) if airline else None,
        }

    def _airport(self, code: str) -> tuple[str, str | None]:
        """(display code, city/name) for a schema-01 airport code."""
        row = self._one("SELECT name, iata, location FROM airports WHERE code=?", code)
        if row is None:
            return code, None
        return row["iata"] or code, row["location"] or row["name"] or None

    async def fetch_aircraft(self, mode_s: str) -> dict | None:
        """Airframe details by hex. Sparse: the position feed's r/t/desc
        fields remain the primary airframe source. No photos (CC0 data
        carries none; product mode shows none anyway)."""
        row = self._one("SELECT manufacturer, model, model_icao, operator"
                        " FROM aircraft WHERE icao=?", mode_s.upper())
        if row is None:
            return None
        return {
            "manufacturer": row["manufacturer"] or None,
            "model": row["model"] or row["model_icao"] or None,
            "owner": row["operator"] or None,
            "country": None,
            "photo": None,
            "photo_thumb": None,
        }

    async def fetch_airline(self, icao_prefix: str) -> dict | None:
        """Airline by 3-letter ICAO callsign prefix."""
        row = self._one("SELECT name, iata FROM airlines WHERE icao=? LIMIT 1",
                        icao_prefix.upper())
        if row is None:
            return None
        return {"airline": row["name"], "airline_iata": row["iata"] or None}
