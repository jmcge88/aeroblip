"""SQLite registry of sold devices and their API tokens.

Tokens are generated and provisioned into each unit by tools/flash_product.py,
which registers them here via POST /api/devices/register. Validation is an
in-memory set lookup; last-seen updates are throttled and written back to
SQLite so the fleet can be inspected with GET /api/devices.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time


class DeviceRegistry:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS devices ("
            "token TEXT PRIMARY KEY, name TEXT, created INTEGER,"
            "last_seen INTEGER, fw TEXT)")
        self._db.commit()
        self._lock = threading.Lock()
        self._tokens = {row[0] for row in self._db.execute("SELECT token FROM devices")}
        self._touched: dict[str, float] = {}

    def register(self, token: str, name: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO devices (token, name, created) VALUES (?, ?, ?)",
                (token, name, int(time.time())))
            self._db.commit()
            self._tokens.add(token)

    def valid(self, token: str | None) -> bool:
        return bool(token) and token in self._tokens

    def touch(self, token: str, fw: str | None = None) -> None:
        """Record device activity, at most once a minute per token."""
        now = time.monotonic()
        if now - self._touched.get(token, 0) < 60:
            return
        self._touched[token] = now
        with self._lock:
            self._db.execute("UPDATE devices SET last_seen = ?, fw = COALESCE(?, fw) "
                             "WHERE token = ?", (int(time.time()), fw, token))
            self._db.commit()

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT token, name, created, last_seen, fw FROM devices ORDER BY created").fetchall()
        return [{"token": r[0], "name": r[1], "created": r[2], "last_seen": r[3], "fw": r[4]}
                for r in rows]
