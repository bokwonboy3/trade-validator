"""Pytest configuration — register custom markers and skip rules.

Markers:
  live   — touches the real Binance API or external network. Skipped by default
           to keep `pytest tests/` fast and offline. Run manually with
           `pytest -m live` or `pytest --run-live`.

Auto-disable agentic tier in tests unless explicitly opted in:
  Default scan/integration tests should NOT spawn the real claude CLI.
  Set AGENT_BACKEND=none in the test environment by default; tests that need
  agentic analysis can override with their own monkeypatch.
"""
from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run live tests that hit the real Binance API or external services.",
    )


@pytest.fixture(autouse=True)
def _disable_agent_backend_by_default(monkeypatch):
    """Force agent backend off for every test, unless the test explicitly
    re-enables it via its own monkeypatch. Prevents the test suite from
    spawning a real `claude` CLI subprocess (which is slow and may make a
    network call against the user's plan quota)."""
    # Only set if not already overridden by the test itself.
    if "AGENT_BACKEND" not in os.environ:
        monkeypatch.setenv("AGENT_BACKEND", "none")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: marks tests that require real network access (deselect with '-m \"not live\"')",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live") or any(
        m in config.getoption("-m", default="") for m in ("live",)
    ):
        return
    skip_live = pytest.mark.skip(reason="needs --run-live or -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
