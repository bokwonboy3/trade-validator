"""CLI surface tests for scan.py — args parsing + exit codes via mocked scan()."""
from __future__ import annotations

from pathlib import Path

import pytest

import scan
from alert_state import AlertState
from scan import (
    EXIT_ALL_SYMBOLS_FAILED,
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    ScanResult,
    parse_args,
    run_scan,
)
from scanner_config import (
    FileChannelConfig,
    NotificationsConfig,
    ScannerConfig,
    TelegramChannelConfig,
    ThresholdsConfig,
)


def _cfg(symbols=("BTCUSDT",), min_score=4) -> ScannerConfig:
    return ScannerConfig(
        symbols=tuple(symbols),
        thresholds=ThresholdsConfig(min_score=min_score, default_rr=3.0),
        notifications=NotificationsConfig(
            channels=("stdout",),
            file=FileChannelConfig(path="alerts.log"),
            telegram=TelegramChannelConfig(enabled=False),
        ),
    )


def test_parse_args_defaults():
    a = parse_args([])
    assert a.config is None
    assert a.state == ".tv-state.json"
    assert a.once is False
    assert a.quiet is False


def test_parse_args_all_flags():
    a = parse_args(["--config", "x.toml", "--state", "/tmp/s.json", "--once", "--quiet"])
    assert a.config == "x.toml"
    assert a.state == "/tmp/s.json"
    assert a.once is True
    assert a.quiet is True


def test_run_scan_returns_ok_when_no_passing(mocker, tmp_path, capsys):
    # All symbols return non-passing results
    mocker.patch.object(
        scan,
        "scan",
        return_value=[
            ScanResult(symbol="BTCUSDT", skipped_reason="no clear 4H trend"),
        ],
    )
    state = AlertState(path=tmp_path / "s.json")
    rc = run_scan(_cfg(), state=state)
    assert rc == EXIT_OK
    captured = capsys.readouterr()
    assert "Scan Summary" in captured.out
    assert "skipped" in captured.out


def test_run_scan_returns_ok_when_all_setups_suppressed(mocker, tmp_path):
    """When idempotency suppresses every passing setup, exit OK + no dispatch."""
    from analysis.layers import LayerResult, SetupEvaluation
    from analysis.scanner_logic import SynthesizedSetup

    setup = SynthesizedSetup(
        direction="long", entry=80_000, sl=79_700, tp=80_900, sl_swing_price=79_900
    )
    ev = SetupEvaluation(
        layer_1=LayerResult(1, "pass", {"ma25": 1, "ma99": 0}),
        layer_2=LayerResult(1, "pass", {"closest_label": "x", "closest_price": 1, "distance_pct": 0.001}),
        layer_3=LayerResult(1, "pass", {"rejection_volume": 1}),
        layer_4=LayerResult(1, "pass", {"passed_swing_price": 79_900, "distance_pct": 0.001}),
        layer_5=LayerResult(1, "pass", {"rr": 3.0, "reward": 1, "risk": 1, "min_rr": 3.0}),
    )
    mocker.patch.object(
        scan,
        "scan",
        return_value=[ScanResult(symbol="BTCUSDT", setup=setup, evaluation=ev)],
    )

    # Pre-record alert with the tier-prefixed signature used by scan.run_scan
    state = AlertState(path=tmp_path / "s.json")
    state.record("confirmed:BTCUSDT", "confirmed:long", 79_900)

    dispatch_spy = mocker.patch.object(scan, "dispatch")
    rc = run_scan(_cfg(), state=state)
    assert rc == EXIT_OK
    dispatch_spy.assert_not_called()


def test_run_scan_returns_failure_when_all_symbols_error(mocker, tmp_path, capsys):
    mocker.patch.object(
        scan,
        "scan",
        return_value=[
            ScanResult(symbol="BTCUSDT", error="HTTP 500"),
            ScanResult(symbol="ETHUSDT", error="timeout"),
        ],
    )
    state = AlertState(path=tmp_path / "s.json")
    rc = run_scan(_cfg(symbols=("BTCUSDT", "ETHUSDT")), state=state)
    assert rc == EXIT_ALL_SYMBOLS_FAILED
    captured = capsys.readouterr()
    assert "HTTP 500" in captured.err
    assert "timeout" in captured.err


def test_run_scan_partial_errors_still_ok(mocker, tmp_path):
    """If some symbols error but others succeed, return OK (not 3)."""
    mocker.patch.object(
        scan,
        "scan",
        return_value=[
            ScanResult(symbol="BTCUSDT", error="HTTP 500"),
            ScanResult(symbol="ETHUSDT", skipped_reason="no clear trend"),
        ],
    )
    state = AlertState(path=tmp_path / "s.json")
    rc = run_scan(_cfg(symbols=("BTCUSDT", "ETHUSDT")), state=state)
    assert rc == EXIT_OK


def test_run_scan_quiet_suppresses_summary(mocker, tmp_path, capsys):
    mocker.patch.object(
        scan,
        "scan",
        return_value=[ScanResult(symbol="BTCUSDT", skipped_reason="no clear trend")],
    )
    state = AlertState(path=tmp_path / "s.json")
    run_scan(_cfg(), state=state, quiet=True)
    captured = capsys.readouterr()
    assert "Scan Summary" not in captured.out
    assert "skipped" not in captured.out  # full skip line suppressed in quiet


def test_run_scan_quiet_still_emits_errors_to_stderr(mocker, tmp_path, capsys):
    mocker.patch.object(
        scan,
        "scan",
        return_value=[
            ScanResult(symbol="BTCUSDT", error="HTTP 500"),
            ScanResult(symbol="ETHUSDT", skipped_reason="no clear trend"),
        ],
    )
    state = AlertState(path=tmp_path / "s.json")
    run_scan(_cfg(symbols=("BTCUSDT", "ETHUSDT")), state=state, quiet=True)
    captured = capsys.readouterr()
    # Even in quiet mode, individual errors go to stderr
    assert "HTTP 500" in captured.err


def test_main_returns_config_error_for_bad_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config.toml here
    rc = scan.main(["--config", str(tmp_path / "nope.toml")])
    assert rc == EXIT_CONFIG_ERROR
