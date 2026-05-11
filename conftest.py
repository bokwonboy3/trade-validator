"""Pytest configuration — register custom markers and skip rules.

Markers:
  live   — touches the real Binance API or external network. Skipped by default
           to keep `pytest tests/` fast and offline. Run manually with
           `pytest -m live` or `pytest --run-live`.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run live tests that hit the real Binance API or external services.",
    )


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
