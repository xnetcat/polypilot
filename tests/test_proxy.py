"""The live CLOB trading client routes through POLYPILOT_HTTP_PROXY when set."""
import polymarket_mcp.execution as ex


class _FakeClob:
    def set_api_creds(self, creds): pass
    def create_or_derive_api_key(self): return {}


def test_clob_client_installs_proxy_when_configured(monkeypatch):
    monkeypatch.setattr(ex.config, "HTTP_PROXY", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr("py_clob_client_v2.client.ClobClient", lambda **k: _FakeClob())
    import py_clob_client_v2.http_helpers.helpers as helpers
    original = helpers._http_client
    ex._clob_client = None
    try:
        ex._get_clob_client()
        # py_clob_client's module-global http client was replaced with a proxied one
        assert helpers._http_client is not original
    finally:
        helpers._http_client = original
        ex._clob_client = None


def test_config_exports_standard_proxy_env(monkeypatch):
    # Setting POLYPILOT_HTTP_PROXY exports the standard HTTPS_PROXY/HTTP_PROXY
    # (which httpx honors) so ALL requests in the process route through it.
    import importlib
    import os
    import polymarket_mcp.config as cfg
    monkeypatch.setenv("POLYPILOT_HTTP_PROXY", "http://u:p@9.9.9.9:8000")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    try:
        importlib.reload(cfg)
        assert cfg.HTTP_PROXY == "http://u:p@9.9.9.9:8000"
        assert os.environ.get("HTTPS_PROXY") == "http://u:p@9.9.9.9:8000"
        assert os.environ.get("HTTP_PROXY") == "http://u:p@9.9.9.9:8000"
    finally:
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
        monkeypatch.delenv("POLYPILOT_HTTP_PROXY", raising=False)
        importlib.reload(cfg)


def test_geoblock_error_returns_actionable_proxy_hint(monkeypatch):
    # A 403 geoblock on order placement must surface a clear proxy/restart hint,
    # not a raw exception string.
    monkeypatch.setattr(ex.config, "POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(ex.config, "HTTP_PROXY", "")   # proxy NOT applied

    class _Client:
        def create_market_order(self, args): return {"signed": True}
        def post_order(self, order, otype):
            raise Exception("PolyApiException[status_code=403, error_message="
                            "{'error': 'Trading restricted in your region'}]")
    monkeypatch.setattr(ex, "_get_clob_client", lambda: _Client())

    res = ex._live_fill("m", "tok", "q", "weather", "YES", 5.0, 0.6, 0.6, "t",
                        "market", None)
    assert res["status"] == "error"
    assert "geoblock" in res["reason"].lower()
    assert "NOT SET" in res["reason"] and "RESTART" in res["reason"]


def test_clob_client_untouched_without_proxy(monkeypatch):
    monkeypatch.setattr(ex.config, "HTTP_PROXY", "")
    monkeypatch.setattr("py_clob_client_v2.client.ClobClient", lambda **k: _FakeClob())
    import py_clob_client_v2.http_helpers.helpers as helpers
    original = helpers._http_client
    ex._clob_client = None
    try:
        ex._get_clob_client()
        assert helpers._http_client is original   # no proxy -> not replaced
    finally:
        ex._clob_client = None
