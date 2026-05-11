"""Verify scan.py and validate.py work under a stripped (cron-like) environment.

Cron strips most env vars (no PYTHONPATH, no PATH from shell). These tests
simulate that with `env -i` and confirm the scripts still import and run.

Network-dependent runs are guarded behind the `live` marker; the import-only
test runs everywhere.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
MINIMAL_ENV = {"HOME": os.environ.get("HOME", "/tmp"), "PATH": "/usr/bin:/bin"}


def _run_in_minimal_env(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), *args],
        env=MINIMAL_ENV,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.skipif(not PYTHON.exists(), reason=f"{PYTHON} not found — run from a fresh venv")
def test_validate_help_in_minimal_env():
    """argparse `--help` works without PATH/PYTHONPATH inherited from a shell."""
    r = _run_in_minimal_env(["validate.py", "--help"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "5-Layer" in r.stdout


@pytest.mark.skipif(not PYTHON.exists(), reason=f"{PYTHON} not found")
def test_scan_help_in_minimal_env():
    r = _run_in_minimal_env(["scan.py", "--help"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "Multi-symbol" in r.stdout


@pytest.mark.skipif(not PYTHON.exists(), reason=f"{PYTHON} not found")
def test_scan_imports_load_in_minimal_env():
    """Every module imported by scan.py loads under stripped env (no implicit PYTHONPATH).
    Catches scenarios where the codebase accidentally depends on a shell-set var."""
    code = """
import scan
import validate
from analysis.layers import evaluate_setup
from analysis.scanner_logic import synthesize_setup, determine_direction
from data.binance import fetch_klines, BinanceError
from output.formatter import format_report, ValidationReport
from output.notify import build_channels, dispatch
from scanner_config import load_config
from alert_state import AlertState
import sys
sys.exit(0)
"""
    r = _run_in_minimal_env(["-c", code])
    assert r.returncode == 0, f"stderr: {r.stderr}"


@pytest.mark.live
@pytest.mark.skipif(not PYTHON.exists(), reason=f"{PYTHON} not found")
def test_scan_real_run_in_minimal_env(tmp_path):
    """Full scan.py run in a stripped env hitting real Binance API. Catches network +
    config + filesystem interaction under cron-like conditions."""
    state = tmp_path / "state.json"
    r = _run_in_minimal_env(
        [
            "scan.py",
            "--config",
            "config.example.toml",
            "--state",
            str(state),
            "--quiet",
        ],
        timeout=30.0,
    )
    # Exit 0 = ran successfully (regardless of whether any setup passed)
    # Exit 3 = all symbols failed (Binance outage) — tolerate as flaky network
    assert r.returncode in (0, 3), f"unexpected exit {r.returncode}, stderr: {r.stderr}"
    # State file must be created (with valid JSON) even if no alerts dispatched
    if r.returncode == 0:
        assert state.exists()
        import json
        body = json.loads(state.read_text())
        assert "alerts" in body
