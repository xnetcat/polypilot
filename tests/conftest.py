"""Test-suite baseline: always run in paper mode.

This dev machine may export POLYPILOT_MODE=live (for real trading), which would
otherwise leak into the test process and break tests that rely on the default
mode. Pin paper here before any config import; live-path tests override per-test
via monkeypatch.
"""
import os

os.environ["POLYPILOT_MODE"] = "paper"
