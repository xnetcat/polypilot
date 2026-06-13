"""The live CLOB trading client routes through POLYPILOT_HTTP_PROXY when set."""
import polymarket_mcp.execution as ex


class _FakeClob:
    def set_api_creds(self, creds): pass
    def create_or_derive_api_creds(self): return {}


def test_clob_client_installs_proxy_when_configured(monkeypatch):
    monkeypatch.setattr(ex.config, "HTTP_PROXY", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr("py_clob_client.client.ClobClient", lambda **k: _FakeClob())
    import py_clob_client.http_helpers.helpers as helpers
    original = helpers._http_client
    ex._clob_client = None
    try:
        ex._get_clob_client()
        # py_clob_client's module-global http client was replaced with a proxied one
        assert helpers._http_client is not original
    finally:
        helpers._http_client = original
        ex._clob_client = None


def test_clob_client_untouched_without_proxy(monkeypatch):
    monkeypatch.setattr(ex.config, "HTTP_PROXY", "")
    monkeypatch.setattr("py_clob_client.client.ClobClient", lambda **k: _FakeClob())
    import py_clob_client.http_helpers.helpers as helpers
    original = helpers._http_client
    ex._clob_client = None
    try:
        ex._get_clob_client()
        assert helpers._http_client is original   # no proxy -> not replaced
    finally:
        ex._clob_client = None
