"""Tests for monitor.py — SL/TP touch, trend reversal, /status calc.

Strategy: never hit Binance. Mock kline_fetcher with synthetic DataFrames.
Each test creates a fresh tmp journal.db and seeds the needed alerts/trades.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

import monitor
from data.journal_db import (
    close_trade,
    connect,
    insert_alert_idempotent,
    record_trade,
)
from monitor import (
    MonitorState,
    OpenTradeStatus,
    ReversalAlertRecord,
    check_sl_tp_touches,
    check_trend_reversals,
    compute_open_trade_status,
    run_position_monitor,
)


@pytest.fixture(autouse=True)
def _enable_monitor(monkeypatch):
    """Monitor tests need MONITOR_DISABLED off."""
    monkeypatch.delenv("MONITOR_DISABLED", raising=False)


@pytest.fixture
def tmpdb(tmp_path) -> Path:
    return tmp_path / "j.db"


def _alert(alert_id: str, **overrides) -> dict:
    base = {
        "alert_id": alert_id,
        "ts": "2026-05-11T04:30:00+00:00",
        "symbol": "BTCUSDT",
        "direction": "long",
        "tier": "confirmed",
        "entry": 80000.0,
        "sl": 79700.0,
        "tp": 80900.0,
        "tier1_score": 4,
        "agent_verdict": "ENTER",
        "agent_confidence": 75,
        "agent_downgraded": False,
    }
    base.update(overrides)
    return base


def _klines_df(highs, lows, closes=None) -> pd.DataFrame:
    """Build a minimal klines DF with the columns SL/TP check reads."""
    n = len(highs)
    closes = closes or highs
    return pd.DataFrame({
        "openTime": list(range(n)),
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1.0] * n,
        "closeTime": list(range(1, n + 1)),
    })


# --- SL/TP touch ---
def test_sl_touch_long_closes_trade(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
            position_size=100,
        )

    # 1m candle low touched 79700
    fake_fetch = lambda s: _klines_df(highs=[80100, 80050], lows=[79900, 79650])

    captured = []
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(
            conn, dispatcher=captured.append, kline_fetcher=fake_fetch,
        )
    assert len(events) == 1
    assert events[0].kind == "sl_hit"
    assert events[0].detail["exit_price"] == 79700
    with connect(tmpdb) as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=1").fetchone()
    assert row["status"] == "closed"
    assert row["close_reason"] == "sl_auto"
    assert any("SL hit" in m for m in captured)


def test_tp_touch_long_closes_trade(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    # high reached 80900
    fake_fetch = lambda s: _klines_df(highs=[80800, 80950], lows=[80700, 80850])
    captured = []
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(
            conn, dispatcher=captured.append, kline_fetcher=fake_fetch,
        )
    assert len(events) == 1
    assert events[0].kind == "tp_hit"
    assert events[0].detail["exit_price"] == 80900
    assert any("TP hit" in m for m in captured)


def test_sl_takes_precedence_over_tp_when_both_in_window(tmpdb):
    """Whipsaw: same window touched both. SL wins (conservative)."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    fake_fetch = lambda s: _klines_df(highs=[80950], lows=[79600])  # both touched
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(conn, kline_fetcher=fake_fetch)
    assert len(events) == 1
    assert events[0].kind == "sl_hit"


def test_sl_touch_short_uses_high(tmpdb):
    """For SHORT: SL is above entry, so check high >= sl."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1", direction="short"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=80300, filled_tp=79100,
        )
    fake_fetch = lambda s: _klines_df(highs=[80350], lows=[80100])
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(conn, kline_fetcher=fake_fetch)
    assert len(events) == 1
    assert events[0].kind == "sl_hit"


def test_tp_touch_short_uses_low(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1", direction="short"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=80300, filled_tp=79100,
        )
    fake_fetch = lambda s: _klines_df(highs=[79500], lows=[79050])
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(conn, kline_fetcher=fake_fetch)
    assert len(events) == 1
    assert events[0].kind == "tp_hit"


def test_no_touch_no_event(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    fake_fetch = lambda s: _klines_df(highs=[80100], lows=[79800])
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(conn, kline_fetcher=fake_fetch)
    assert events == []
    with connect(tmpdb) as conn:
        row = conn.execute("SELECT status FROM trades").fetchone()
    assert row["status"] == "open"


def test_closed_trades_not_re_checked(tmpdb):
    """Already-closed trades excluded by open_trades()."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        tid = record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
        close_trade(conn, trade_id=tid, exit_price=80100)
    fake_fetch = lambda s: _klines_df(highs=[80900], lows=[79700])  # would touch both
    with connect(tmpdb) as conn:
        events = check_sl_tp_touches(conn, kline_fetcher=fake_fetch)
    assert events == []


# --- MonitorState ---
def test_monitor_state_load_missing(tmp_path):
    state = MonitorState.load(tmp_path / "missing.json")
    assert state.alerted_reversals == {}


def test_monitor_state_save_load_roundtrip(tmp_path):
    p = tmp_path / "ms.json"
    s = MonitorState(path=p)
    s.record(1, "short")
    s.save()
    s2 = MonitorState.load(p)
    assert "1" in s2.alerted_reversals
    assert s2.alerted_reversals["1"].reverse_to == "short"


def test_already_alerted_matches_direction(tmp_path):
    s = MonitorState(path=tmp_path / "ms.json")
    s.record(1, "short")
    assert s.already_alerted(1, "short")
    assert not s.already_alerted(1, "long")
    assert not s.already_alerted(2, "short")


def test_monitor_state_prunes_old(tmp_path):
    from datetime import datetime, timedelta, timezone
    p = tmp_path / "ms.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    p.write_text('{"alerted_reversals": {"1": {"alerted_at": "' + old + '", "reverse_to": "short"}}}')
    s = MonitorState.load(p)
    assert s.alerted_reversals == {}


# --- Trend reversal ---
def _trend_4h_df(ma25_first: bool) -> pd.DataFrame:
    """Build a 4H DF where MA25 ends above or below MA99.

    Two scenarios:
      ma25_first=True  → recent closes elevated → MA25 > MA99 (LONG trend)
      ma25_first=False → recent closes depressed → MA25 < MA99 (SHORT trend)
    """
    n = 120
    base = 80000
    closes = []
    for i in range(n):
        if i < n - 30:
            closes.append(base)
        else:
            closes.append(base + 500 if ma25_first else base - 500)
    return pd.DataFrame({
        "openTime": list(range(n)),
        "open": closes,
        "high": [c + 10 for c in closes],
        "low": [c - 10 for c in closes],
        "close": closes,
        "volume": [1.0] * n,
        "closeTime": list(range(1, n + 1)),
    })


def test_trend_reversal_fires_when_4h_flips(tmpdb):
    """LONG trade + 4H trend now SHORT → alert with buttons."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    state = MonitorState(path=tmpdb.with_suffix(".monitor.json"))
    button_calls = []
    fake_4h = lambda s: _trend_4h_df(ma25_first=False)  # SHORT trend now
    with connect(tmpdb) as conn:
        events = check_trend_reversals(
            conn, state,
            dispatcher_with_buttons=lambda m, kb: button_calls.append((m, kb)),
            klines_4h_fetcher=fake_4h,
        )
    assert len(events) == 1
    assert events[0].kind == "trend_reversal"
    assert events[0].detail["new_trend"] == "short"
    assert len(button_calls) == 1
    text, kb = button_calls[0]
    assert "추세 반전" in text
    # buttons present
    flat = [b for row in kb for b in row]
    assert any(b["callback_data"].startswith("close_now:") for b in flat)
    assert any(b["callback_data"].startswith("observe:") for b in flat)
    # State recorded → idempotency
    assert state.already_alerted(1, "short")


def test_trend_reversal_idempotent_second_call(tmpdb):
    """Same state, same reversal — second call doesn't re-fire."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    state = MonitorState(path=tmpdb.with_suffix(".monitor.json"))
    fake_4h = lambda s: _trend_4h_df(ma25_first=False)
    calls = []
    with connect(tmpdb) as conn:
        check_trend_reversals(
            conn, state,
            dispatcher_with_buttons=lambda m, kb: calls.append(m),
            klines_4h_fetcher=fake_4h,
        )
        # Second invocation
        check_trend_reversals(
            conn, state,
            dispatcher_with_buttons=lambda m, kb: calls.append(m),
            klines_4h_fetcher=fake_4h,
        )
    assert len(calls) == 1  # only first fired


def test_trend_reversal_no_alert_when_trend_aligned(tmpdb):
    """LONG trade + 4H trend still LONG → no alert."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    state = MonitorState(path=tmpdb.with_suffix(".monitor.json"))
    fake_4h = lambda s: _trend_4h_df(ma25_first=True)  # LONG trend
    calls = []
    with connect(tmpdb) as conn:
        events = check_trend_reversals(
            conn, state,
            dispatcher_with_buttons=lambda m, kb: calls.append(m),
            klines_4h_fetcher=fake_4h,
        )
    assert events == []
    assert calls == []


def test_trend_reversal_one_call_per_symbol(tmpdb, mocker):
    """Two trades on same symbol → only one 4H fetch."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        insert_alert_idempotent(conn, _alert("A2"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
        record_trade(
            conn, alert_id="A2",
            filled_entry=80100, filled_sl=79800, filled_tp=81000,
        )
    state = MonitorState(path=tmpdb.with_suffix(".monitor.json"))
    fetcher = mocker.Mock(return_value=_trend_4h_df(ma25_first=False))
    with connect(tmpdb) as conn:
        events = check_trend_reversals(
            conn, state,
            dispatcher_with_buttons=lambda m, kb: None,
            klines_4h_fetcher=fetcher,
        )
    assert fetcher.call_count == 1  # deduped by symbol
    assert len(events) == 2  # two trades alerted


# --- Open trade status ---
def test_status_long_unrealized_pnl(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
            position_size=1000,
        )
    # current price = 80400
    fake = lambda s: _klines_df(highs=[80400], lows=[80400], closes=[80400])
    with connect(tmpdb) as conn:
        statuses = compute_open_trade_status(conn, kline_fetcher=fake)
    assert len(statuses) == 1
    s = statuses[0]
    assert s.current_price == 80400.0
    assert s.unrealized_pnl_pct == pytest.approx(0.5, rel=1e-3)
    assert s.unrealized_pnl_usd == pytest.approx(5.0, rel=1e-3)
    assert s.distance_to_sl_pct > 0  # we're above SL
    assert s.distance_to_tp_pct > 0  # we're below TP


def test_status_short_pnl_signs(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1", direction="short"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=80300, filled_tp=79100,
        )
    fake = lambda s: _klines_df(highs=[79600], lows=[79600], closes=[79600])
    with connect(tmpdb) as conn:
        statuses = compute_open_trade_status(conn, kline_fetcher=fake)
    assert statuses[0].unrealized_pnl_pct > 0  # short profitable when price falls


def test_status_empty_when_no_open(tmpdb):
    with connect(tmpdb) as conn:
        statuses = compute_open_trade_status(conn, kline_fetcher=lambda s: _klines_df([80000], [80000]))
    assert statuses == []


# --- Orchestrator ---
def test_run_position_monitor_disabled_env(monkeypatch, tmpdb):
    monkeypatch.setenv("MONITOR_DISABLED", "1")
    events = run_position_monitor(db_path=tmpdb)
    assert events == []


def test_run_position_monitor_combines_events(tmpdb, monkeypatch, mocker):
    monkeypatch.delenv("MONITOR_DISABLED", raising=False)
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _alert("A1"))
        record_trade(
            conn, alert_id="A1",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    mocker.patch(
        "monitor._fetch_latest_1m",
        return_value=_klines_df(highs=[80100], lows=[79650]),  # SL hit
    )
    mocker.patch(
        "monitor._fetch_4h",
        return_value=_trend_4h_df(ma25_first=True),  # trend still aligned
    )
    state = MonitorState(path=tmpdb.with_suffix(".monitor.json"))
    events = run_position_monitor(db_path=tmpdb, state=state)
    kinds = [e.kind for e in events]
    assert "sl_hit" in kinds
