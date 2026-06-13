"""Best-effort outbound notifications.

This module is a strict NO-OP when no channel is configured: with neither
``POLYPILOT_WEBHOOK_URL`` nor a complete Telegram pair set, ``notify`` returns
immediately WITHOUT touching the network. That keeps the test suite hermetic
(zero network) by default.

Every HTTP send is wrapped in try/except: a notification failure must NEVER
raise or change the caller's control flow. ``notify`` always returns a small
summary dict describing what was (attempted to be) sent.
"""
from __future__ import annotations

import httpx

from . import config


def _webhook_configured() -> bool:
    return bool(config.POLYPILOT_WEBHOOK_URL)


def _telegram_configured() -> bool:
    return bool(config.POLYPILOT_TELEGRAM_BOT_TOKEN and config.POLYPILOT_TELEGRAM_CHAT_ID)


def notify(event: str, message: str, **fields) -> dict:
    """Send a best-effort notification to every configured channel.

    Returns immediately with ``{"sent": False, "reason": "no channel
    configured"}`` (and makes NO network call) when neither a webhook nor a
    complete Telegram config is present. Otherwise POSTs to each configured
    channel and returns ``{"sent": bool, "channels": [...]}`` where each entry
    records the channel and whether its individual send succeeded.

    Never raises: any HTTP/transport error is captured per-channel and surfaced
    in that channel's ``ok``/``error`` fields, not propagated.
    """
    if not _webhook_configured() and not _telegram_configured():
        return {"sent": False, "reason": "no channel configured"}

    channels: list[dict] = []
    if _webhook_configured():
        channels.append(_send_webhook(event, message, fields))
    if _telegram_configured():
        channels.append(_send_telegram(event, message))

    return {"sent": any(c.get("ok") for c in channels), "channels": channels}


def _send_webhook(event: str, message: str, fields: dict) -> dict:
    """POST a JSON ``{event, message, **fields}`` to the configured webhook."""
    payload = {"event": event, "message": message, **fields}
    try:
        resp = httpx.post(config.POLYPILOT_WEBHOOK_URL, json=payload, timeout=10)
        ok = 200 <= getattr(resp, "status_code", 0) < 300
        return {"channel": "webhook", "ok": ok,
                "status_code": getattr(resp, "status_code", None)}
    except Exception as e:
        return {"channel": "webhook", "ok": False, "error": f"{type(e).__name__}: {e}"}


def _send_telegram(event: str, message: str) -> dict:
    """POST to the Telegram Bot API ``sendMessage`` endpoint."""
    url = (f"https://api.telegram.org/bot{config.POLYPILOT_TELEGRAM_BOT_TOKEN}"
           "/sendMessage")
    body = {"chat_id": config.POLYPILOT_TELEGRAM_CHAT_ID,
            "text": f"[{event}] {message}"}
    try:
        resp = httpx.post(url, json=body, timeout=10)
        ok = 200 <= getattr(resp, "status_code", 0) < 300
        return {"channel": "telegram", "ok": ok,
                "status_code": getattr(resp, "status_code", None)}
    except Exception as e:
        return {"channel": "telegram", "ok": False, "error": f"{type(e).__name__}: {e}"}
