"""Test-suite baseline: always run in paper mode.

This dev machine may export POLYPILOT_MODE=live (for real trading), which would
otherwise leak into the test process and break tests that rely on the default
mode. Pin paper here before any config import; live-path tests override per-test
via monkeypatch.
"""
import os

os.environ["POLYPILOT_MODE"] = "paper"
# Never let an ambient proxy (this dev machine may export POLYPILOT_HTTP_PROXY
# for live trading) activate during tests — config would otherwise export
# HTTPS_PROXY and route mocked requests oddly. Keep the suite fully hermetic.
for _v in ("POLYPILOT_HTTP_PROXY", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_v, None)
