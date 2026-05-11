"""Tests for telegram_listener — callback dispatch, multi-step flows, state.

Strategy: never hit the real Telegram API. Patch `_tg` so we capture outgoing
messages, and feed synthetic Update dicts into `process_update`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import telegram_listener as tl
from data.journal_db import (
    connect,
    insert_alert_idempotent,
    record_trade,
)


@pytest.fixture
def tmpdb(tmp_path) -> Path:
    return tmp_path / "j.db"


@pytest.fixture
def alert_in_db(tmpdb):
    """Insert one sample alert; return its id."""
    alert_id = "BTC-202605110430-confirmed"
    record = {
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
    with connect(tmpdb) as conn:
        insert_alert_idempotent(conn, record)
    return alert_id


@pytest.fixture
def captured(monkeypatch):
    """Capture all outgoing Telegram API calls. Returns a list of (method, payload)."""
    calls: list[tuple[str, dict]] = []

    def fake_tg(method, token, **payload):
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(tl, "_tg", fake_tg)
    return calls


def _send_texts(calls) -> list[str]:
    """Extract just the text bodies from sendMessage calls."""
    return [p["text"] for m, p in calls if m == "sendMessage"]


# --- ConvState round-trip ---
def test_conv_state_json_roundtrip():
    c = tl.ConvState(awaiting="entry", alert_id="X-1", partial={"entry": 80000.0})
    c2 = tl.ConvState.from_json(c.to_json())
    assert c2.awaiting == "entry"
    assert c2.alert_id == "X-1"
    assert c2.partial == {"entry": 80000.0}


def test_conv_state_reset_clears_all():
    c = tl.ConvState(awaiting="sl", alert_id="X-1", partial={"entry": 80000.0})
    c.reset()
    assert c.awaiting is None
    assert c.alert_id is None
    assert c.partial == {}


def test_load_state_returns_default_when_missing(tmp_path):
    state = tl.load_state(tmp_path / "missing.json")
    assert state == {"offset": 0, "chats": {}}


def test_save_then_load_state(tmp_path):
    p = tmp_path / "state.json"
    tl.save_state({"offset": 42, "chats": {"99": {"awaiting": "tp"}}}, p)
    loaded = tl.load_state(p)
    assert loaded["offset"] == 42
    assert loaded["chats"]["99"]["awaiting"] == "tp"


# --- Callback dispatch ---
def test_callback_take_market_records_trade(captured, tmpdb, alert_in_db):
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-1",
            "data": f"take_market:{alert_in_db}",
            "message": {"chat": {"id": 12345}},
        },
    }
    conv_states: dict = {}
    tl.process_update(
        update, token="T", db_path=tmpdb,
        conv_states=conv_states, default_size=100.0,
    )
    # answerCallbackQuery + sendMessage
    methods = [m for m, _ in captured]
    assert "answerCallbackQuery" in methods
    assert "sendMessage" in methods
    # Trade was written
    with connect(tmpdb) as conn:
        rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 1
    assert rows[0]["filled_entry"] == 80000.0
    assert rows[0]["position_size"] == 100.0
    # Confirmation mentions trade id
    assert any("Trade #" in t for t in _send_texts(captured))


def test_callback_take_market_unknown_alert(captured, tmpdb):
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-2",
            "data": "take_market:NONEXISTENT",
            "message": {"chat": {"id": 12345}},
        },
    }
    tl.process_update(
        update, token="T", db_path=tmpdb, conv_states={}, default_size=None,
    )
    assert any("없음" in t for t in _send_texts(captured))


def test_callback_take_custom_sets_awaiting_entry(captured, tmpdb, alert_in_db):
    conv_states: dict = {}
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-3",
            "data": f"take_custom:{alert_in_db}",
            "message": {"chat": {"id": 12345}},
        },
    }
    tl.process_update(
        update, token="T", db_path=tmpdb,
        conv_states=conv_states, default_size=None,
    )
    conv = conv_states["12345"]
    assert conv.awaiting == "entry"
    assert conv.alert_id == alert_in_db
    assert any("진입 가격" in t for t in _send_texts(captured))


def test_callback_skip_sets_awaiting_skip_reason(captured, tmpdb, alert_in_db):
    conv_states: dict = {}
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-4",
            "data": f"skip:{alert_in_db}",
            "message": {"chat": {"id": 12345}},
        },
    }
    tl.process_update(
        update, token="T", db_path=tmpdb,
        conv_states=conv_states, default_size=None,
    )
    conv = conv_states["12345"]
    assert conv.awaiting == "skip_reason"
    assert conv.alert_id == alert_in_db


def test_callback_unknown_action_replies(captured, tmpdb):
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-5",
            "data": "garbage:xxx",
            "message": {"chat": {"id": 12345}},
        },
    }
    tl.process_update(
        update, token="T", db_path=tmpdb, conv_states={}, default_size=None,
    )
    assert any("알 수 없는 action" in t for t in _send_texts(captured))


# --- Multi-step custom take flow ---
def test_custom_take_full_flow_records_trade(captured, tmpdb, alert_in_db):
    """entry → sl → tp → size → trade inserted."""
    conv_states: dict = {}
    # Start with callback (sets awaiting=entry)
    tl.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "data": f"take_custom:{alert_in_db}",
                "message": {"chat": {"id": 12345}},
            },
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    # Send entry
    tl.process_update(
        {
            "update_id": 2,
            "message": {"chat": {"id": 12345}, "text": "80050"},
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    assert conv_states["12345"].awaiting == "sl"
    # Send SL
    tl.process_update(
        {
            "update_id": 3,
            "message": {"chat": {"id": 12345}, "text": "79700"},
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    assert conv_states["12345"].awaiting == "tp"
    # Send TP
    tl.process_update(
        {
            "update_id": 4,
            "message": {"chat": {"id": 12345}, "text": "80900"},
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    assert conv_states["12345"].awaiting == "size"
    # Send size (positive value)
    tl.process_update(
        {
            "update_id": 5,
            "message": {"chat": {"id": 12345}, "text": "200"},
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    # Conv reset
    assert conv_states["12345"].awaiting is None
    # Trade exists
    with connect(tmpdb) as conn:
        rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 1
    assert rows[0]["filled_entry"] == 80050
    assert rows[0]["filled_sl"] == 79700
    assert rows[0]["filled_tp"] == 80900
    assert rows[0]["position_size"] == 200


def test_custom_take_size_zero_means_no_size(captured, tmpdb, alert_in_db):
    """Size=0 skips position size (None)."""
    conv = tl.ConvState(
        awaiting="size", alert_id=alert_in_db,
        partial={"entry": 80050, "sl": 79700, "tp": 80900},
    )
    tl.handle_reply(
        token="T", chat_id=12345, text="0",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    with connect(tmpdb) as conn:
        rows = conn.execute("SELECT * FROM trades").fetchall()
    assert rows[0]["position_size"] is None


def test_custom_take_non_numeric_rejected(captured, tmpdb, alert_in_db):
    conv = tl.ConvState(awaiting="entry", alert_id=alert_in_db, partial={})
    tl.handle_reply(
        token="T", chat_id=12345, text="not a number",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert conv.awaiting == "entry"  # still waiting
    assert any("숫자만" in t for t in _send_texts(captured))


def test_cancel_resets_conv(captured, tmpdb, alert_in_db):
    conv = tl.ConvState(
        awaiting="sl", alert_id=alert_in_db, partial={"entry": 80050},
    )
    tl.handle_reply(
        token="T", chat_id=12345, text="/cancel",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert conv.awaiting is None
    assert conv.alert_id is None
    assert conv.partial == {}


# --- Skip reason flow ---
def test_skip_reason_records_skip(captured, tmpdb, alert_in_db):
    conv = tl.ConvState(awaiting="skip_reason", alert_id=alert_in_db, partial={})
    tl.handle_reply(
        token="T", chat_id=12345, text="저항 너무 가까움",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert conv.awaiting is None
    with connect(tmpdb) as conn:
        row = conn.execute(
            "SELECT * FROM skipped WHERE alert_id = ?", (alert_in_db,)
        ).fetchone()
    assert row["reason"] == "저항 너무 가까움"


# --- /close command ---
def test_close_command_closes_trade(captured, tmpdb, alert_in_db):
    with connect(tmpdb) as conn:
        tid = record_trade(
            conn, alert_id=alert_in_db,
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
            position_size=100,
        )
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text=f"/close {tid} 80800 tp_near",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    with connect(tmpdb) as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "closed"
    assert row["exit_price"] == 80800
    assert any("PnL" in t for t in _send_texts(captured))


def test_close_command_bad_format(captured, tmpdb):
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/close 1",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert any("사용법" in t for t in _send_texts(captured))


def test_close_command_nonnumeric(captured, tmpdb):
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/close abc def",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert any("잘못된 형식" in t for t in _send_texts(captured))


def test_close_command_unknown_trade(captured, tmpdb):
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/close 999 80000",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    # Should send error from ValueError
    assert any("❌" in t for t in _send_texts(captured))


# --- /open command ---
def test_open_command_lists_trades(captured, tmpdb, alert_in_db):
    with connect(tmpdb) as conn:
        record_trade(
            conn, alert_id=alert_in_db,
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
        )
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/open",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    texts = _send_texts(captured)
    assert any("BTCUSDT" in t for t in texts)
    assert any("long" in t for t in texts)


def test_open_command_empty(captured, tmpdb):
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/open",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert any("열린 trade 없음" in t for t in _send_texts(captured))


# --- No-context reply ---
def test_reply_without_conv_shows_help(captured, tmpdb):
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="hello",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert any("대화 컨텍스트 없음" in t for t in _send_texts(captured))


# --- Message handler integration ---
def test_message_update_dispatches_to_reply(captured, tmpdb, alert_in_db):
    conv_states: dict = {
        "12345": tl.ConvState(awaiting="entry", alert_id=alert_in_db, partial={}),
    }
    tl.process_update(
        {
            "update_id": 7,
            "message": {"chat": {"id": 12345}, "text": "80050"},
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    assert conv_states["12345"].awaiting == "sl"


def test_empty_message_text_ignored(captured, tmpdb):
    conv_states: dict = {}
    tl.process_update(
        {
            "update_id": 7,
            "message": {"chat": {"id": 12345}},  # no text key
        },
        token="T", db_path=tmpdb, conv_states=conv_states, default_size=None,
    )
    # Nothing should be sent
    assert _send_texts(captured) == []


# --- Phase 5: close_now / observe callbacks ---
def test_close_now_callback_closes_trade(captured, tmpdb, alert_in_db, mocker):
    """🔴 즉시 종료 button: fetches current price, closes trade."""
    import pandas as pd
    from data.journal_db import record_trade
    with connect(tmpdb) as conn:
        record_trade(
            conn, alert_id=alert_in_db,
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
            position_size=100,
        )
    # Mock the price fetch inside handle_close_now
    mocker.patch(
        "data.binance.fetch_klines",
        return_value=pd.DataFrame({
            "openTime": [0], "open": [80300], "high": [80350], "low": [80250],
            "close": [80300], "volume": [1.0], "closeTime": [1],
        }),
    )
    tl.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-close",
                "data": "close_now:1",
                "message": {"chat": {"id": 12345}},
            },
        },
        token="T", db_path=tmpdb, conv_states={}, default_size=None,
    )
    with connect(tmpdb) as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=1").fetchone()
    assert row["status"] == "closed"
    assert row["close_reason"] == "reversal_close"
    assert any("즉시 종료" in t for t in _send_texts(captured))


def test_close_now_unknown_trade(captured, tmpdb):
    tl.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb",
                "data": "close_now:999",
                "message": {"chat": {"id": 12345}},
            },
        },
        token="T", db_path=tmpdb, conv_states={}, default_size=None,
    )
    assert any("없거나 이미 종료" in t for t in _send_texts(captured))


def test_close_now_bad_id_format(captured, tmpdb):
    tl.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb",
                "data": "close_now:abc",
                "message": {"chat": {"id": 12345}},
            },
        },
        token="T", db_path=tmpdb, conv_states={}, default_size=None,
    )
    assert any("파싱 실패" in t for t in _send_texts(captured))


def test_observe_callback_just_acknowledges(captured, tmpdb):
    tl.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb",
                "data": "observe:42",
                "message": {"chat": {"id": 12345}},
            },
        },
        token="T", db_path=tmpdb, conv_states={}, default_size=None,
    )
    texts = _send_texts(captured)
    assert any("관찰 유지" in t for t in texts)
    assert any("42" in t for t in texts)


# --- Phase 5: /status command ---
def test_status_command_empty(captured, tmpdb, monkeypatch):
    monkeypatch.delenv("MONITOR_DISABLED", raising=False)
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/status",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    assert any("열린 trade 없음" in t for t in _send_texts(captured))


def test_status_command_with_open_trade(captured, tmpdb, alert_in_db, mocker, monkeypatch):
    import pandas as pd
    from data.journal_db import record_trade
    monkeypatch.delenv("MONITOR_DISABLED", raising=False)
    with connect(tmpdb) as conn:
        record_trade(
            conn, alert_id=alert_in_db,
            filled_entry=80000, filled_sl=79700, filled_tp=80900,
            position_size=100,
        )
    # Patch the kline call inside compute_open_trade_status via the function name
    mocker.patch(
        "monitor._fetch_latest_close_1m",
        return_value=pd.DataFrame({
            "openTime": [0], "open": [80400], "high": [80450], "low": [80350],
            "close": [80400], "volume": [1.0], "closeTime": [1],
        }),
    )
    conv = tl.ConvState()
    tl.handle_reply(
        token="T", chat_id=12345, text="/status",
        conv=conv, db_path=tmpdb, default_size=None,
    )
    texts = _send_texts(captured)
    body = "\n".join(texts)
    assert "BTCUSDT" in body
    assert "80400" in body or "80,400" in body
    # Long entered at 80000, now 80400 → +0.5%
    assert "+0.50%" in body
