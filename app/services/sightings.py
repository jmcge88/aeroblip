"""Spotting log: flyovers, airframes seen and recent tracks, in SQLite.

The server watches every aircraft anyway; this remembers them. Three tables
per location cell (the same ~5 km grid cells services.hub pools pollers on):

- flyovers:     one row per overhead-ring entry - the headline event, kept
                forever (a year of busy-airport flyovers is a few MB).
- seen:         one row per airframe ever seen in the area - the "life list".
                A revisit after SESSION_GAP_S counts as a new sighting.
- track_points: thinned position samples for the heatmap/replay view, purged
                after TRACK_RETENTION_HOURS.

Writes happen inline on the poll loop: a poll touches a few dozen rows in one
WAL transaction, well under a millisecond of work per poll.

DEMO_MODE writes to a separate database file so fabricated Qantas flights
never contaminate a real spotting log.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SESSION_GAP_S = 1800        # same airframe again after this = a new sighting
REVISIT_S = 1800            # same airframe overhead again after this = new flyover
TRACK_POINT_INTERVAL_S = 30
PURGE_EVERY_S = 3600
# A brand-new database would fire a "first time seeing this type" event for
# every single aircraft; seed silently for the first day instead.
NEW_TYPE_SEED_S = 24 * 3600
MAX_TRACK_POINTS = 40000    # response cap for the replay/heatmap endpoint

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flyovers (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  cell TEXT NOT NULL,
  hex TEXT, callsign TEXT, registration TEXT, type TEXT, description TEXT,
  airline TEXT, airline_iata TEXT, origin TEXT, destination TEXT,
  altitude_ft INTEGER, ground_speed_kt INTEGER, track REAL, distance_nm REAL
);
CREATE INDEX IF NOT EXISTS flyovers_cell_ts ON flyovers(cell, ts);
CREATE TABLE IF NOT EXISTS seen (
  cell TEXT NOT NULL, hex TEXT NOT NULL,
  registration TEXT, type TEXT, description TEXT, airline_iata TEXT,
  first_ts INTEGER, last_ts INTEGER, count INTEGER,
  PRIMARY KEY (cell, hex)
);
CREATE TABLE IF NOT EXISTS track_points (
  ts INTEGER NOT NULL, cell TEXT NOT NULL, hex TEXT NOT NULL,
  lat REAL, lon REAL, altitude_ft INTEGER,
  callsign TEXT, origin TEXT, destination TEXT
);
CREATE INDEX IF NOT EXISTS track_cell_ts ON track_points(cell, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _local_midnight() -> int:
    now = datetime.now().astimezone()
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class Sightings:
    def __init__(self, db_path: str | Path, retention_hours: float = 72):
        self._retention_s = retention_hours * 3600
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # A database from before callsign/route were tracked per point: add the
        # columns in place rather than force a fresh table (OperationalError
        # means a prior run already migrated it).
        for col in ("callsign", "origin", "destination"):
            try:
                self._conn.execute(f"ALTER TABLE track_points ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        self._conn.execute("INSERT OR IGNORE INTO meta VALUES ('created_at', ?)",
                           (str(int(time.time())),))
        self._conn.commit()
        row = self._conn.execute("SELECT value FROM meta WHERE key='created_at'").fetchone()
        self._created_at = float(row["value"])
        # Per-(cell, hex) memory so a poll costs no reads for known aircraft
        self._overhead: dict[tuple, float] = {}    # last time seen inside the ring
        self._seen: dict[tuple, dict] = {}         # {"last_ts", "count", "written"}
        self._last_point: dict[tuple, float] = {}

    def record(self, cell: str, aircraft: list[dict]) -> list[dict]:
        """Log one poll's aircraft for one cell. Returns events worth telling
        someone about: [{"kind": "flyover"|"new_type", "aircraft": {...}}]."""
        now = int(time.time())
        events: list[dict] = []
        try:
            for a in aircraft:
                hexcode = a.get("hex")
                if not hexcode:
                    continue
                key = (cell, hexcode)
                events.extend(self._record_seen(key, a, now))
                if a.get("overhead"):
                    last = self._overhead.get(key)
                    if last is None or now - last > REVISIT_S:
                        self._insert_flyover(cell, a, now)
                        events.append({"kind": "flyover", "aircraft": a})
                    self._overhead[key] = now
                if (a.get("lat") is not None
                        and now - self._last_point.get(key, 0) >= TRACK_POINT_INTERVAL_S):
                    self._last_point[key] = now
                    route = a.get("route") or {}
                    self._conn.execute(
                        "INSERT INTO track_points (ts, cell, hex, lat, lon, altitude_ft,"
                        " callsign, origin, destination) VALUES (?,?,?,?,?,?,?,?,?)",
                        (now, cell, hexcode, a["lat"], a["lon"], a.get("altitude_ft"),
                         a.get("callsign"), route.get("origin"), route.get("destination")))
            self._conn.commit()
        except sqlite3.Error:
            log.exception("sightings write failed")
        if len(self._overhead) > 5000:  # bound the memory maps, not the log
            cutoff = now - 4 * REVISIT_S
            for d in (self._overhead, self._last_point):
                for k, ts in list(d.items()):
                    if ts < cutoff:
                        del d[k]
            for k, s in list(self._seen.items()):
                if s["last_ts"] < cutoff:
                    del self._seen[k]
        return events

    def _record_seen(self, key: tuple, a: dict, now: int) -> list[dict]:
        state = self._seen.get(key)
        if state is None:  # not seen since boot - check the log
            row = self._conn.execute(
                "SELECT last_ts, count FROM seen WHERE cell=? AND hex=?",
                key).fetchone()
            if row is not None:
                state = {"last_ts": row["last_ts"], "count": row["count"],
                         "written": 0}
        events: list[dict] = []
        if state is None:
            new_type = self._is_new_type(key[0], a.get("type"))
            self._conn.execute(
                "INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?,?,?,?,?)",
                (*key, a.get("registration"), a.get("type"), a.get("description"),
                 (a.get("airline") or {}).get("airline_iata"), now, now, 1))
            self._seen[key] = {"last_ts": now, "count": 1, "written": now}
            if new_type and now - self._created_at > NEW_TYPE_SEED_S:
                events.append({"kind": "new_type", "aircraft": a})
            return events
        if now - state["last_ts"] > SESSION_GAP_S:
            state["count"] += 1
        state["last_ts"] = now
        if now - state["written"] >= 60:  # throttle the per-poll UPDATE churn
            self._conn.execute(
                "UPDATE seen SET last_ts=?, count=? WHERE cell=? AND hex=?",
                (now, state["count"], *key))
            state["written"] = now
        self._seen[key] = state
        return events

    def _is_new_type(self, cell: str, actype: str | None) -> bool:
        if not actype:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM seen WHERE cell=? AND type=? LIMIT 1", (cell, actype)).fetchone()
        return row is None

    def _insert_flyover(self, cell: str, a: dict, now: int) -> None:
        route = a.get("route") or {}
        airline = a.get("airline") or {}
        self._conn.execute(
            "INSERT INTO flyovers (ts, cell, hex, callsign, registration, type,"
            " description, airline, airline_iata, origin, destination,"
            " altitude_ft, ground_speed_kt, track, distance_nm)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, cell, a.get("hex"), a.get("callsign"), a.get("registration"),
             a.get("type"), a.get("description"),
             airline.get("airline") or route.get("airline"),
             airline.get("airline_iata") or route.get("airline_iata"),
             route.get("origin"), route.get("destination"),
             a.get("altitude_ft"), a.get("ground_speed_kt"), a.get("track"),
             a.get("distance_nm")))

    def _reader(self) -> sqlite3.Connection:
        """A fresh read-only connection: stats/tracks queries run in worker
        threads (asyncio.to_thread) while the poll loop writes on the main
        thread, and sharing one connection across threads is only safe on
        serialized SQLite builds. WAL makes concurrent readers free."""
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def stats(self, cell: str) -> dict:
        """Daily and all-time spotting statistics for one cell."""
        mid = _local_midnight()
        conn = self._reader()
        q = conn.execute
        hourly = [0] * 24
        for row in q("SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INT) h,"
                     " COUNT(*) c FROM flyovers WHERE cell=? AND ts>=? GROUP BY h",
                     (cell, mid)):
            hourly[row["h"]] = row["c"]
        busiest = max(range(24), key=lambda h: hourly[h]) if any(hourly) else None

        def toplist(sql, *params, limit=5):
            return [dict(r) for r in q(sql + f" LIMIT {limit}", params)]

        today = {
            "flyovers": q("SELECT COUNT(*) c FROM flyovers WHERE cell=? AND ts>=?",
                          (cell, mid)).fetchone()["c"],
            "unique_aircraft": q("SELECT COUNT(*) c FROM seen WHERE cell=? AND last_ts>=?",
                                 (cell, mid)).fetchone()["c"],
            "busiest_hour": f"{busiest:02d}:00" if busiest is not None else None,
            "hourly": hourly,
            "top_types": toplist(
                "SELECT type, MAX(description) description, COUNT(*) c FROM flyovers"
                " WHERE cell=? AND ts>=? AND type IS NOT NULL GROUP BY type"
                " ORDER BY c DESC", cell, mid),
            "top_airlines": toplist(
                "SELECT airline, airline_iata, COUNT(*) c FROM flyovers"
                " WHERE cell=? AND ts>=? AND airline IS NOT NULL GROUP BY airline"
                " ORDER BY c DESC", cell, mid),
            "top_routes": toplist(
                "SELECT origin, destination, COUNT(*) c FROM flyovers"
                " WHERE cell=? AND ts>=? AND origin IS NOT NULL GROUP BY"
                " origin, destination ORDER BY c DESC", cell, mid),
        }
        first = q("SELECT MIN(first_ts) f FROM seen WHERE cell=?", (cell,)).fetchone()["f"]
        alltime = {
            "since": first,
            "flyovers": q("SELECT COUNT(*) c FROM flyovers WHERE cell=?",
                          (cell,)).fetchone()["c"],
            "unique_aircraft": q("SELECT COUNT(*) c FROM seen WHERE cell=?",
                                 (cell,)).fetchone()["c"],
            "unique_types": q("SELECT COUNT(DISTINCT type) c FROM seen"
                              " WHERE cell=? AND type IS NOT NULL", (cell,)).fetchone()["c"],
            "unique_airlines": q("SELECT COUNT(DISTINCT airline_iata) c FROM flyovers"
                                 " WHERE cell=? AND airline_iata IS NOT NULL",
                                 (cell,)).fetchone()["c"],
            "top_types": toplist(
                "SELECT type, MAX(description) description, COUNT(*) c FROM flyovers"
                " WHERE cell=? AND type IS NOT NULL GROUP BY type ORDER BY c DESC",
                cell, limit=8),
            "rarest_types": toplist(
                "SELECT type, MAX(description) description,"
                " COUNT(*) airframes, SUM(count) c FROM seen"
                " WHERE cell=? AND type IS NOT NULL AND type != '' GROUP BY type"
                " ORDER BY c ASC, MAX(last_ts) DESC", cell),
            "recent_first_types": toplist(
                "SELECT type, MAX(description) description, MIN(first_ts) f FROM seen"
                " WHERE cell=? AND type IS NOT NULL AND type != '' GROUP BY type"
                " ORDER BY f DESC", cell),
        }
        conn.close()
        return {"cell": cell, "today": today, "alltime": alltime}

    def tracks(self, cell: str, hours: float) -> dict:
        """Recent position samples for the heatmap/replay map, oldest first."""
        since = int(time.time() - hours * 3600)
        conn = self._reader()
        try:
            rows = conn.execute(
                "SELECT ts, hex, lat, lon, altitude_ft, callsign, origin, destination"
                " FROM track_points WHERE cell=? AND ts>=? ORDER BY ts LIMIT ?",
                (cell, since, MAX_TRACK_POINTS)).fetchall()
        finally:
            conn.close()
        return {"since": since,
                "truncated": len(rows) == MAX_TRACK_POINTS,
                "points": [[r["ts"], r["hex"], r["lat"], r["lon"], r["altitude_ft"],
                            r["callsign"], r["origin"], r["destination"]] for r in rows]}

    async def run(self) -> None:
        """Purge expired track points (flyovers and the life list are forever)."""
        while True:
            try:
                cutoff = int(time.time() - self._retention_s)
                cur = self._conn.execute("DELETE FROM track_points WHERE ts<?", (cutoff,))
                self._conn.commit()
                if cur.rowcount:
                    log.info("sightings: purged %d expired track points", cur.rowcount)
            except sqlite3.Error:
                log.exception("sightings purge failed")
            await asyncio.sleep(PURGE_EVERY_S)

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()
