# tests/test_notifications.py
"""Tests for the best-effort notifier.

Hermetic by design: when unconfigured, notify() makes ZERO network calls. The
configured-channel cases monkeypatch httpx.post so no real request is issued.
"""
import importlib

import pytest


@pytest.fixture
def notif(monkeypatch):
    # Start from a clean, fully-unconfigured config so the default state is a
    # genuine no-op regardless of the developer's environment/.env.
    for var in ("POLYPILOT_WEBHOOK_URL", "POLYPILOT_TELEGRAM_BOT_TOKEN",
                "POLYPILOT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    import polymarket_mcp.config as cfg
    import polymarket_mcp.notifications as notifications
    importlib.reload(cfg)
    importlib.reload(notifications)
    return notifications


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Unconfigured -> no-op, no network
# --------------------------------------------------------------------------- #
def test_notify_noop_when_unconfigured(notif, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("notify must not touch the network when unconfigured")
    monkeypatch.setattr(notif.httpx, "post", _boom)

    res = notif.notify("fill", "bought YES @ 0.50")
    assert res == {"sent": False, "reason": "no channel configured"}


# --------------------------------------------------------------------------- #
# Webhook configured -> POSTs a JSON {event, message, ...}
# --------------------------------------------------------------------------- #
def test_notify_posts_to_webhook(notif, monkeypatch):
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://hooks.example.com/abc")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(notif.httpx, "post", fake_post)

    res = notif.notify("fill", "bought Lakers @ 0.40", shares=50, mode="paper")
    assert res["sent"] is True
    assert any(c["channel"] == "webhook" and c["ok"] for c in res["channels"])
    # The webhook receives the structured payload.
    assert captured["url"] == "https://hooks.example.com/abc"
    assert captured["json"]["event"] == "fill"
    assert captured["json"]["message"] == "bought Lakers @ 0.40"
    assert captured["json"]["shares"] == 50
    assert captured["json"]["mode"] == "paper"


# --------------------------------------------------------------------------- #
# Telegram configured -> POSTs to sendMessage
# --------------------------------------------------------------------------- #
def test_notify_posts_to_telegram(notif, monkeypatch):
    monkeypatch.setattr(notif.config, "POLYPILOT_TELEGRAM_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setattr(notif.config, "POLYPILOT_TELEGRAM_CHAT_ID", "12345")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(notif.httpx, "post", fake_post)

    res = notif.notify("breaker", "Kill switch tripped")
    assert res["sent"] is True
    assert "api.telegram.org/botBOTTOKEN/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "12345"
    assert "breaker" in captured["json"]["text"]
    assert "Kill switch tripped" in captured["json"]["text"]


def test_telegram_requires_both_token_and_chat_id(notif, monkeypatch):
    # Token without chat id is NOT a configured channel -> still a no-op.
    monkeypatch.setattr(notif.config, "POLYPILOT_TELEGRAM_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setattr(notif.config, "POLYPILOT_TELEGRAM_CHAT_ID", "")

    def _boom(*a, **k):
        raise AssertionError("incomplete telegram config must not send")
    monkeypatch.setattr(notif.httpx, "post", _boom)

    res = notif.notify("fill", "x")
    assert res == {"sent": False, "reason": "no channel configured"}


# --------------------------------------------------------------------------- #
# Never raises on a failing endpoint
# --------------------------------------------------------------------------- #
def test_notify_never_raises_on_failure(notif, monkeypatch):
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://hooks.example.com/abc")

    def _boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(notif.httpx, "post", _boom)

    res = notif.notify("fill", "anything")  # must not raise
    assert res["sent"] is False
    ch = next(c for c in res["channels"] if c["channel"] == "webhook")
    assert ch["ok"] is False
    assert "error" in ch


def test_notify_non_2xx_status_marks_not_ok(notif, monkeypatch):
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://hooks.example.com/abc")
    monkeypatch.setattr(notif.httpx, "post",
                        lambda url, json=None, timeout=None: _Resp(500))
    res = notif.notify("fill", "anything")
    assert res["sent"] is False
    ch = next(c for c in res["channels"] if c["channel"] == "webhook")
    assert ch["ok"] is False
    assert ch["status_code"] == 500


def test_discord_webhook_uses_content_payload(notif, monkeypatch):
    # A Discord webhook URL must receive {"content": ...} (what Discord expects),
    # NOT the generic {event, message} shape, and 204 counts as success.
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://discord.com/api/webhooks/123/abc")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp(204)   # Discord returns 204 No Content on success

    monkeypatch.setattr(notif.httpx, "post", fake_post)
    res = notif.notify("fill", "bought Lakers @ 0.40", shares=50)
    assert res["sent"] is True
    assert "content" in captured["json"]          # Discord shape
    assert "event" not in captured["json"]        # NOT the generic shape
    assert "fill" in captured["json"]["content"]
    assert "bought Lakers @ 0.40" in captured["json"]["content"]
    assert "shares=50" in captured["json"]["content"]


def test_notify_both_channels(notif, monkeypatch):
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://hooks.example.com/abc")
    monkeypatch.setattr(notif.config, "POLYPILOT_TELEGRAM_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setattr(notif.config, "POLYPILOT_TELEGRAM_CHAT_ID", "12345")
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(url)
        return _Resp(200)

    monkeypatch.setattr(notif.httpx, "post", fake_post)
    res = notif.notify("fill", "msg")
    assert res["sent"] is True
    channels = sorted(c["channel"] for c in res["channels"])
    assert channels == ["telegram", "webhook"]
    assert len(posts) == 2
