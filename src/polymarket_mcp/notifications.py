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
        channels.append(_send_telegram(event, message, fields))

    return {"sent": any(c.get("ok") for c in channels), "channels": channels}


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url


# Per-event embed presentation. Titles carry an emoji + short label; colors are
# Discord-friendly decimal RGB ints. Anything not listed falls back to a
# title-cased event name and the neutral "blurple" brand color.
_EMBED_TITLES = {
    "fill": "✅ Fill",
    "settled": "📊 Settled",
    "breaker": "🛑 Kill switch",
    "status": "📈 Status",
    "error": "⚠️ Error",
}
_EMBED_COLORS = {
    "fill": 0x2ECC71,     # green
    "settled": 0x3498DB,  # blue
    "breaker": 0xE74C3C,  # red
    "status": 0x95A5A6,   # grey
    "error": 0xE67E22,    # orange
}
_DEFAULT_COLOR = 0x7289DA  # Discord blurple

# Discord embed limits we defend against.
_MAX_EMBED_FIELDS = 25
_MAX_FIELD_VALUE = 1024


def _discord_embed(event: str, message: str, fields: dict) -> dict:
    """Build a single Discord embed payload (``{"embeds": [{...}]}``).

    Maps the event to a title/color, uses ``message`` as the description, and
    renders each kwarg as an inline embed field. Field count is capped at 25 and
    each value truncated to Discord's 1024-char limit."""
    embed_fields = []
    for k, v in list(fields.items())[:_MAX_EMBED_FIELDS]:
        embed_fields.append({
            "name": str(k),
            "value": str(v)[:_MAX_FIELD_VALUE],
            "inline": True,
        })
    embed = {
        "title": _EMBED_TITLES.get(event, event.title()),
        "description": message,
        "color": _EMBED_COLORS.get(event, _DEFAULT_COLOR),
    }
    if embed_fields:
        embed["fields"] = embed_fields
    return {"embeds": [embed]}


def _send_webhook(event: str, message: str, fields: dict) -> dict:
    """POST to the configured webhook. Discord webhooks get a rich ``embeds``
    payload (title/color per event, message as description, kwargs as inline
    fields); any other URL receives the generic ``{event, message, **fields}``
    JSON shape."""
    url = config.POLYPILOT_WEBHOOK_URL
    if _is_discord(url):
        payload = _discord_embed(event, message, fields)
    else:
        payload = {"event": event, "message": message, **fields}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        ok = 200 <= getattr(resp, "status_code", 0) < 300
        return {"channel": "webhook", "ok": ok,
                "status_code": getattr(resp, "status_code", None)}
    except Exception as e:
        return {"channel": "webhook", "ok": False, "error": f"{type(e).__name__}: {e}"}


def _send_telegram(event: str, message: str, fields: dict | None = None) -> dict:
    """POST to the Telegram Bot API ``sendMessage`` endpoint.

    Plain-text body of ``[event] message``; any kwargs are appended one per line
    as ``k: v`` for a little extra context."""
    url = (f"https://api.telegram.org/bot{config.POLYPILOT_TELEGRAM_BOT_TOKEN}"
           "/sendMessage")
    text = f"[{event}] {message}"
    if fields:
        text += "\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())
    body = {"chat_id": config.POLYPILOT_TELEGRAM_CHAT_ID,
            "text": text}
    try:
        resp = httpx.post(url, json=body, timeout=10)
        ok = 200 <= getattr(resp, "status_code", 0) < 300
        return {"channel": "telegram", "ok": ok,
                "status_code": getattr(resp, "status_code", None)}
    except Exception as e:
        return {"channel": "telegram", "ok": False, "error": f"{type(e).__name__}: {e}"}
