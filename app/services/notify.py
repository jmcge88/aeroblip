"""Push notifications shared by every feature that tells your phone something.

One place for the ntfy/webhook plumbing (config.NTFY_URL / config.WEBHOOK_URL)
so watch matches, follow-a-flight alerts and the monthly digest all behave
identically: fire-and-forget, never stalling a poll loop, failures logged and
swallowed.
"""
from __future__ import annotations

import logging

import httpx

from app import config

log = logging.getLogger(__name__)


async def push(client: httpx.AsyncClient, title: str, message: str, *,
               priority: str = "default", tags: str = "small_airplane",
               event: dict | None = None) -> None:
    """Send one notification to whichever channels are configured.

    `event` (optional) is included verbatim in the webhook JSON body for
    consumers that want structure, matching the shape watches always sent:
    {"title", "message", "event"}.
    """
    if config.NTFY_URL:
        try:
            await client.post(
                config.NTFY_URL, content=message.encode(),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                timeout=10)
        except Exception as exc:
            log.warning("ntfy push failed: %s", exc)
    if config.WEBHOOK_URL:
        try:
            await client.post(
                config.WEBHOOK_URL,
                json={"title": title, "message": message, "event": event},
                timeout=10)
        except Exception as exc:
            log.warning("webhook push failed: %s", exc)
