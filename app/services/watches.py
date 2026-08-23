"""Watch rules: get told when interesting aircraft appear - no Home Assistant
needed.

Rules live in DATA_DIR/watches.json and are evaluated inside every location
poller's loop against the same enriched aircraft the displays show. A match
produces an event that:

- rides the websocket to connected dashboards (toast + optional voice), via
  the "watch_events" list embedded in overhead snapshots, and
- is pushed to a phone through ntfy (config.NTFY_URL - the free ntfy app
  subscribed to a private topic receives real push notifications), and/or a
  generic JSON webhook (config.WEBHOOK_URL).

Rule fields (see FIELDS): match on callsign prefix, registration, hex, type
or airline, or on the built-in detectors - "circling" (see poller), "squawk"
(any 7500/7600/7700 in the area) and "new_type" (first-ever sighting of a
type, from the spotting log). Modifiers: within_nm, overhead_only,
golden_only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path

import httpx

from app import config

log = logging.getLogger(__name__)

FIELDS = ("callsign", "registration", "hex", "type", "airline",
          "circling", "squawk", "new_type")
VALUE_RE = re.compile(r"^[A-Za-z0-9 .\-]{0,32}$")
MAX_RULES = 50
# Don't re-notify the same (rule, airframe) pair while it hangs around; a
# news helicopter matching "circling" for an hour is one event, not sixty.
COOLDOWN_S = 6 * 3600
RECENT_KEEP_S = 900   # how long an event stays embedded in snapshots
MAX_RECENT = 20


def _clean(a: dict) -> dict:
    """Notification payloads keep the useful facts, not the whole dict."""
    return {k: a.get(k) for k in
            ("hex", "callsign", "registration", "type", "description",
             "altitude_ft", "ground_speed_kt", "distance_nm",
             "bearing_from_home", "heading_cardinal", "phase", "squawk",
             "circling", "overhead", "route", "airline")}


def _describe(a: dict) -> str:
    airline = (a.get("airline") or {}).get("airline")
    route = a.get("route") or {}
    bits = [a.get("description") or a.get("type"),
            airline,
            f"{route['origin']}→{route['destination']}"
            if route.get("origin") and route.get("destination") else None,
            f"{round(a['altitude_ft']):,} ft" if a.get("altitude_ft") is not None else None,
            f"{a['distance_nm']:.1f} NM {a.get('heading_cardinal') or ''}".strip()
            if a.get("distance_nm") is not None else None]
    return " · ".join(str(b) for b in bits if b)


class WatchManager:
    def __init__(self, path: str | Path, client: httpx.AsyncClient):
        self._path = Path(path)
        self._client = client
        self._rules: list[dict] = []
        self._fired: dict[tuple, float] = {}   # (rule_id, hex) -> last notify
        self._recent: dict[str, list[dict]] = {}  # cell -> recent events
        try:
            if self._path.exists():
                self._rules = json.loads(self._path.read_text())["rules"]
        except (OSError, ValueError, KeyError):
            log.exception("watches file unreadable - starting empty")

    # ---- rule management (REST) ------------------------------------------

    def rules(self) -> list[dict]:
        return self._rules

    def add(self, payload: dict) -> dict:
        field = payload.get("field")
        if field not in FIELDS:
            raise ValueError(f"field must be one of {', '.join(FIELDS)}")
        value = str(payload.get("value") or "").strip().upper()
        if not VALUE_RE.fullmatch(value):
            raise ValueError("value: letters/digits/spaces, max 32 chars")
        if field in ("callsign", "registration", "hex", "type", "airline") and not value:
            raise ValueError("value required for this field")
        if len(self._rules) >= MAX_RULES:
            raise ValueError("too many rules")
        within = payload.get("within_nm")
        rule = {
            "id": secrets.token_urlsafe(8),
            "field": field,
            "value": value,
            "within_nm": min(500.0, max(1.0, float(within))) if within else None,
            "overhead_only": bool(payload.get("overhead_only")),
            "golden_only": bool(payload.get("golden_only")),
        }
        self._rules.append(rule)
        self._save()
        return rule

    def remove(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r["id"] != rule_id]
        if len(self._rules) != before:
            self._save()
            return True
        return False

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"rules": self._rules}, indent=1))
        except OSError:
            log.exception("watches file write failed")

    # ---- evaluation (poll loop) ------------------------------------------

    def recent(self, cell: str) -> list[dict]:
        """Events still worth showing, for embedding in snapshots."""
        cutoff = time.time() - RECENT_KEEP_S
        events = [e for e in self._recent.get(cell, []) if e["ts"] >= cutoff]
        self._recent[cell] = events
        return events

    def check(self, cell: str, aircraft: list[dict], sun: dict,
              sighting_events: list[dict]) -> None:
        """Evaluate every rule against one poll's aircraft. Matches are
        recorded for snapshot embedding and dispatched to notifiers."""
        if not self._rules:
            return
        new_types = {id(e["aircraft"]) for e in sighting_events
                     if e["kind"] == "new_type"}
        now = time.time()
        for rule in self._rules:
            for a in aircraft:
                if not self._matches(rule, a, sun, new_types):
                    continue
                key = (rule["id"], a.get("hex") or a.get("callsign") or "?")
                if now - self._fired.get(key, 0) < COOLDOWN_S:
                    continue
                self._fired[key] = now
                self._fire(cell, rule, a)
        if len(self._fired) > 2000:
            cutoff = now - COOLDOWN_S
            self._fired = {k: t for k, t in self._fired.items() if t >= cutoff}

    def _matches(self, rule: dict, a: dict, sun: dict, new_types: set) -> bool:
        if rule["overhead_only"] and not a.get("overhead"):
            return False
        if rule["golden_only"] and not sun.get("golden"):
            return False
        if (rule["within_nm"] is not None
                and (a.get("distance_nm") is None
                     or a["distance_nm"] > rule["within_nm"])):
            return False
        field, value = rule["field"], rule["value"]
        if field == "circling":
            return bool(a.get("circling"))
        if field == "squawk":
            sq = a.get("squawk")
            e = (a.get("emergency") or "").lower()
            return sq in ("7500", "7600", "7700") or e not in ("", "none", "lifeguard")
        if field == "new_type":
            return id(a) in new_types
        if field == "callsign":
            return (a.get("callsign") or "").upper().startswith(value)
        if field == "registration":
            return (a.get("registration") or "").upper() == value
        if field == "hex":
            return (a.get("hex") or "").upper() == value
        if field == "type":
            return (a.get("type") or "").upper() == value
        if field == "airline":
            al = a.get("airline") or {}
            name = (al.get("airline") or "").upper()
            return value in ((al.get("airline_iata") or "").upper(), ) or value in name
        return False

    def _fire(self, cell: str, rule: dict, a: dict) -> None:
        label = rule["field"] if rule["field"] in ("circling", "squawk", "new_type") \
            else f"{rule['field']}={rule['value']}"
        ident = a.get("callsign") or a.get("registration") or a.get("hex") or "?"
        titles = {"circling": f"{ident} is circling nearby",
                  "squawk": f"{ident} squawking {a.get('squawk') or 'emergency'}",
                  "new_type": f"First {a.get('type') or '?'} ever seen"}
        title = titles.get(rule["field"], f"Watched flight: {ident}")
        event = {
            "id": secrets.token_urlsafe(6),
            "ts": int(time.time()),
            "rule_id": rule["id"],
            "rule": label,
            "kind": rule["field"],
            "title": title,
            "message": _describe(a),
            "aircraft": _clean(a),
        }
        self._recent.setdefault(cell, []).append(event)
        del self._recent[cell][:-MAX_RECENT]
        log.info("watch match [%s]: %s - %s", label, title, event["message"])
        # Fire-and-forget: a slow phone-push service must never stall a poll.
        asyncio.get_running_loop().create_task(self._notify(event))

    async def _notify(self, event: dict) -> None:
        if config.NTFY_URL:
            try:
                await self._client.post(
                    config.NTFY_URL, content=event["message"].encode(),
                    headers={"Title": event["title"],
                             "Priority": "high" if event["kind"] == "squawk" else "default",
                             "Tags": "small_airplane"},
                    timeout=10)
            except Exception as exc:
                log.warning("ntfy push failed: %s", exc)
        if config.WEBHOOK_URL:
            try:
                await self._client.post(
                    config.WEBHOOK_URL,
                    json={"title": event["title"], "message": event["message"],
                          "event": event},
                    timeout=10)
            except Exception as exc:
                log.warning("webhook push failed: %s", exc)
