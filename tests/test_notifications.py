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


def test_discord_webhook_uses_embeds_payload(notif, monkeypatch):
    # A Discord webhook URL must receive a rich {"embeds": [...]} payload (what
    # Discord expects), NOT the generic {event, message} shape, and 204 counts
    # as success.
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://discord.com/api/webhooks/123/abc")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp(204)   # Discord returns 204 No Content on success

    monkeypatch.setattr(notif.httpx, "post", fake_post)
    res = notif.notify("fill", "bought Lakers @ 0.40", shares=50, mode="paper")
    assert res["sent"] is True
    assert "embeds" in captured["json"]           # Discord rich shape
    assert "content" not in captured["json"]      # NOT plain content
    assert "event" not in captured["json"]        # NOT the generic shape

    embed = captured["json"]["embeds"][0]
    assert embed["title"] == "✅ Fill"
    assert embed["description"] == "bought Lakers @ 0.40"
    assert isinstance(embed["color"], int)
    # The passed kwargs become inline embed fields.
    field_names = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_names["shares"] == "50"
    assert field_names["mode"] == "paper"
    assert all(f["inline"] is True for f in embed["fields"])


def test_discord_embed_color_and_title_differ_by_event(notif, monkeypatch):
    # fill (green/✅) and breaker (red/🛑) must produce distinct embeds.
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://discord.com/api/webhooks/123/abc")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp(204)

    monkeypatch.setattr(notif.httpx, "post", fake_post)

    notif.notify("fill", "bought")
    fill_embed = captured["json"]["embeds"][0]
    notif.notify("breaker", "Kill switch tripped")
    breaker_embed = captured["json"]["embeds"][0]

    assert fill_embed["title"] != breaker_embed["title"]
    assert fill_embed["color"] != breaker_embed["color"]
    assert fill_embed["color"] == 0x2ECC71     # green
    assert breaker_embed["color"] == 0xE74C3C  # red
    assert fill_embed["title"] == "✅ Fill"
    assert breaker_embed["title"] == "🛑 Kill switch"


def test_discord_embed_caps_fields_and_truncates_values(notif, monkeypatch):
    # Discord allows at most 25 embed fields and 1024-char values; defend both.
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://discord.com/api/webhooks/123/abc")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp(204)

    monkeypatch.setattr(notif.httpx, "post", fake_post)

    many = {f"k{i}": "x" * 2000 for i in range(40)}
    notif.notify("status", "snapshot", **many)
    embed = captured["json"]["embeds"][0]
    assert len(embed["fields"]) == 25
    assert all(len(f["value"]) <= 1024 for f in embed["fields"])


def test_discord_embed_omits_fields_key_when_no_kwargs(notif, monkeypatch):
    # With no kwargs the embed should not carry an empty "fields" list.
    monkeypatch.setattr(notif.config, "POLYPILOT_WEBHOOK_URL",
                        "https://discord.com/api/webhooks/123/abc")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _Resp(204)

    monkeypatch.setattr(notif.httpx, "post", fake_post)
    notif.notify("error", "something broke")
    embed = captured["json"]["embeds"][0]
    assert "fields" not in embed
    assert embed["title"] == "⚠️ Error"


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
