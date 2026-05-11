"""Tests for SQLite trade journal + CLI."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import journal
from data.journal_db import (
    close_trade,
    closed_trades,
    connect,
    insert_alert_idempotent,
    list_alerts,
    open_trades,
    record_skip,
    record_trade,
)


@pytest.fixture
def tmpdb(tmp_path) -> Path:
    return tmp_path / "j.db"


def _sample_alert(alert_id: str = "BTC-202605110430-confirmed", **overrides) -> dict:
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
        "agent_verdict": "WATCH",
        "agent_confidence": 65,
        "agent_downgraded": True,
    }
    base.update(overrides)
    return base


# --- DB schema + idempotency ---
def test_insert_alert_creates_row(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        rows = list_alerts(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["agent_downgraded"] == 1


def test_insert_alert_idempotent(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        insert_alert_idempotent(conn, _sample_alert())  # same id
        insert_alert_idempotent(conn, _sample_alert())
    with connect(tmpdb) as conn:
        rows = list_alerts(conn)
    assert len(rows) == 1


def test_list_alerts_filters_by_symbol(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert("BTC-1-confirmed"))
        insert_alert_idempotent(conn, _sample_alert("ETH-1-confirmed", symbol="ETHUSDT"))
    with connect(tmpdb) as conn:
        rows = list_alerts(conn, symbol="ETHUSDT")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "ETHUSDT"


# --- Trade lifecycle ---
def test_record_trade_creates_open(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        tid = record_trade(
            conn, alert_id="BTC-202605110430-confirmed",
            filled_entry=80050, filled_sl=79700, filled_tp=80900,
        )
        rows = open_trades(conn)
    assert tid == 1
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["filled_entry"] == 80050


def test_close_trade_long_win_pnl(tmpdb):
    """Long trade closed in profit — pnl_pct positive."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        tid = record_trade(
            conn, alert_id="BTC-202605110430-confirmed",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
            position_size=1000,
        )
        closed = close_trade(conn, trade_id=tid, exit_price=80800, close_reason="tp_near")
    assert closed["status"] == "closed"
    assert closed["pnl_pct"] == pytest.approx(1.0, rel=1e-3)  # +0.8/80 ≈ +1%
    assert closed["pnl_usd"] == pytest.approx(10.0, rel=1e-3)  # 1% of $1000


def test_close_trade_long_loss(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        tid = record_trade(
            conn, alert_id="BTC-202605110430-confirmed",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
        closed = close_trade(conn, trade_id=tid, exit_price=79700, close_reason="sl_hit")
    assert closed["pnl_pct"] == pytest.approx(-0.375, rel=1e-3)


def test_close_trade_short(tmpdb):
    """Short trade — pnl_pct sign inverted."""
    with connect(tmpdb) as conn:
        insert_alert_idempotent(
            conn, _sample_alert("BTC-SHORT-confirmed", direction="short"),
        )
        tid = record_trade(
            conn, alert_id="BTC-SHORT-confirmed",
            filled_entry=80000, filled_sl=80300, filled_tp=79100,
        )
        # Exit at 79200 — favorable for short
        closed = close_trade(conn, trade_id=tid, exit_price=79200, close_reason="near_tp")
    assert closed["pnl_pct"] > 0


def test_close_already_closed_raises(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        tid = record_trade(
            conn, alert_id="BTC-202605110430-confirmed",
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
        close_trade(conn, trade_id=tid, exit_price=80800)
        with pytest.raises(ValueError, match="already"):
            close_trade(conn, trade_id=tid, exit_price=80800)


def test_record_skip(tmpdb):
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, _sample_alert())
        record_skip(conn, alert_id="BTC-202605110430-confirmed", reason="macro bearish")
        row = conn.execute(
            "SELECT * FROM skipped WHERE alert_id = ?",
            ("BTC-202605110430-confirmed",),
        ).fetchone()
    assert dict(row)["reason"] == "macro bearish"


# --- CLI integration ---
def test_cli_migrate_imports_jsonl(tmpdb, tmp_path):
    jsonl = tmp_path / "alerts.jsonl"
    rec = _sample_alert()
    jsonl.write_text(json.dumps(rec) + "\n")
    rc = journal.main([
        "--db", str(tmpdb), "migrate", "--jsonl", str(jsonl),
    ])
    assert rc == 0
    with connect(tmpdb) as conn:
        assert len(list_alerts(conn)) == 1


def test_cli_migrate_idempotent(tmpdb, tmp_path):
    jsonl = tmp_path / "alerts.jsonl"
    jsonl.write_text(json.dumps(_sample_alert()) + "\n")
    journal.main(["--db", str(tmpdb), "migrate", "--jsonl", str(jsonl)])
    journal.main(["--db", str(tmpdb), "migrate", "--jsonl", str(jsonl)])  # again
    with connect(tmpdb) as conn:
        assert len(list_alerts(conn)) == 1


def test_cli_take_then_close_then_stats(tmpdb, tmp_path, capsys):
    jsonl = tmp_path / "alerts.jsonl"
    jsonl.write_text(json.dumps(_sample_alert()) + "\n")
    journal.main(["--db", str(tmpdb), "migrate", "--jsonl", str(jsonl)])

    rc = journal.main([
        "--db", str(tmpdb), "take", "BTC-202605110430-confirmed",
        "--entry", "80050", "--sl", "79700", "--tp", "80900", "--size", "1000",
    ])
    assert rc == 0
    capsys.readouterr()

    rc = journal.main([
        "--db", str(tmpdb), "close", "1", "--price", "80800", "--reason", "tp_near",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PnL" in out

    rc = journal.main(["--db", str(tmpdb), "stats", "--days", "30"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Trades:" in out
    assert "1W" in out  # one win


def test_cli_take_rejects_unknown_alert(tmpdb, capsys):
    rc = journal.main([
        "--db", str(tmpdb), "take", "NONEXISTENT",
        "--entry", "80050", "--sl", "79700", "--tp", "80900",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "없음" in err
