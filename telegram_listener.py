"""Telegram listener daemon — turns phone button taps into journal records.

How it integrates:
1. scan.py dispatches alerts with inline_keyboard (3 buttons + alert_id encoded).
2. User taps button on phone.
3. This daemon long-polls api.telegram.org/bot.../getUpdates and receives the
   callback_query event.
4. It writes to journal.db (data/journal_db.py) and replies to the user.

For multi-step flows (custom entry, skip reason), it maintains conversation
state per chat in a JSON file (`.tv-listener-state.json`).

Loop: long-poll → handle update → save state → repeat.

Usage:
    python telegram_listener.py
    # or run via launchd (see com.user.trade-validator-listener.plist template)
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import requests

from data.journal_db import (
    DEFAULT_DB_PATH,
    close_trade,
    connect,
    insert_alert_idempotent,
    open_trades,
    record_skip,
    record_trade,
)

# --- Config ---
TG_BASE: Final = "https://api.telegram.org"
DEFAULT_STATE_PATH: Final = Path(".tv-listener-state.json")
LONG_POLL_TIMEOUT_SEC: Final = 30  # how long Telegram holds the connection
JOURNAL_DB_ENV: Final = "JOURNAL_DB"
DEFAULT_POSITION_SIZE_ENV: Final = "JOURNAL_DEFAULT_SIZE"


# --- Conversation state ---
@dataclass
class ConvState:
    """Per-chat conversation state. Persisted to JSON so listener restart
    doesn't lose mid-flow conversations."""

    awaiting: str | None = None  # None | "entry" | "sl" | "tp" | "size" | "skip_reason"
    alert_id: str | None = None
    partial: dict[str, Any] = field(default_factory=dict)  # captured values so far

    def to_json(self) -> dict:
        return {"awaiting": self.awaiting, "alert_id": self.alert_id, "partial": self.partial}

    @classmethod
    def from_json(cls, d: dict) -> "ConvState":
        return cls(
            awaiting=d.get("awaiting"),
            alert_id=d.get("alert_id"),
            partial=dict(d.get("partial") or {}),
        )

    def reset(self) -> None:
        self.awaiting = None
        self.alert_id = None
        self.partial = {}


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load offset + per-chat state."""
    if not path.exists():
        return {"offset": 0, "chats": {}}
    return json.loads(path.read_text())


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(path)


# --- Telegram API helpers ---
def _tg(method: str, token: str, **payload) -> dict:
    """POST to Telegram Bot API. Returns body or raises RuntimeError."""
    resp = requests.post(
        f"{TG_BASE}/bot{token}/{method}",
        json=payload,
        timeout=LONG_POLL_TIMEOUT_SEC + 10,
    )
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body}")
    return body


def get_updates(token: str, offset: int) -> list[dict]:
    """Long-poll for new updates since `offset`."""
    body = _tg(
        "getUpdates", token,
        offset=offset, timeout=LONG_POLL_TIMEOUT_SEC,
        allowed_updates=["message", "callback_query"],
    )
    return body.get("result", [])


def send_message(token: str, chat_id: int | str, text: str) -> None:
    _tg("sendMessage", token, chat_id=chat_id, text=text)


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """Acknowledge a button tap (otherwise Telegram shows loading spinner)."""
    _tg("answerCallbackQuery", token, callback_query_id=callback_id, text=text)


# --- Action handlers ---
def _scanner_alert(db_path: Path, alert_id: str) -> dict | None:
    """Fetch alert row from DB for take action (scanner suggested entry/sl/tp)."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
    return dict(row) if row else None


def handle_take_market(
    *, token: str, chat_id: int, alert_id: str, db_path: Path, default_size: float | None,
) -> None:
    """Instant take at scanner-suggested entry/sl/tp."""
    alert = _scanner_alert(db_path, alert_id)
    if not alert:
        send_message(token, chat_id, f"❌ alert {alert_id} 없음. `journal migrate` 먼저 실행 필요.")
        return
    with connect(db_path) as conn:
        tid = record_trade(
            conn, alert_id=alert_id,
            filled_entry=alert["entry"], filled_sl=alert["sl"], filled_tp=alert["tp"],
            position_size=default_size,
            notes="taken via telegram (market)",
        )
    size_str = f" size=${default_size:.0f}" if default_size else ""
    send_message(
        token, chat_id,
        f"✅ Trade #{tid} opened (scanner 가격 사용)\n"
        f"   entry={alert['entry']:.2f} SL={alert['sl']:.2f} TP={alert['tp']:.2f}{size_str}\n"
        f"   종료 시: /close {tid} <exit_price>",
    )


def handle_take_custom_start(
    *, token: str, chat_id: int, alert_id: str, conv: ConvState,
) -> None:
    """Start multi-step flow for custom take."""
    conv.awaiting = "entry"
    conv.alert_id = alert_id
    conv.partial = {}
    send_message(
        token, chat_id,
        f"진입 가격을 답장으로 보내세요 (예: 81250).\n"
        f"중단하려면 /cancel",
    )


def handle_skip_start(
    *, token: str, chat_id: int, alert_id: str, conv: ConvState,
) -> None:
    """Start skip flow — ask for reason."""
    conv.awaiting = "skip_reason"
    conv.alert_id = alert_id
    conv.partial = {}
    send_message(
        token, chat_id,
        f"패스 이유를 답장으로 보내세요 (예: '저항 너무 가까움').\n"
        f"중단하려면 /cancel",
    )


def handle_reply(
    *, token: str, chat_id: int, text: str,
    conv: ConvState, db_path: Path, default_size: float | None,
) -> None:
    """Handle a free-text reply in the context of an active conversation."""
    text = text.strip()

    if text == "/cancel":
        conv.reset()
        send_message(token, chat_id, "취소됨.")
        return

    # /close <trade_id> <exit_price> [reason...]
    if text.startswith("/close "):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            send_message(token, chat_id, "사용법: /close <trade_id> <exit_price> [reason]")
            return
        try:
            tid = int(parts[1])
            price = float(parts[2])
        except ValueError:
            send_message(token, chat_id, "잘못된 형식. 예: /close 1 81600 TP near")
            return
        reason = parts[3] if len(parts) > 3 else "manual"
        try:
            with connect(db_path) as conn:
                closed = close_trade(conn, trade_id=tid, exit_price=price, close_reason=reason)
            pnl = closed["pnl_pct"]
            sign = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            usd = f" (${closed['pnl_usd']:+.2f})" if closed.get("pnl_usd") is not None else ""
            send_message(
                token, chat_id,
                f"✅ Trade #{tid} closed\n"
                f"   exit={price} reason={reason}\n"
                f"   {sign} PnL: {pnl:+.2f}%{usd}",
            )
        except ValueError as e:
            send_message(token, chat_id, f"❌ {e}")
        return

    # /open — list open trades
    if text == "/open":
        with connect(db_path) as conn:
            rows = open_trades(conn)
        if not rows:
            send_message(token, chat_id, "열린 trade 없음.")
            return
        lines = [
            f"#{r['id']} {r['symbol']} {r['direction']} entry={r['filled_entry']:.2f}"
            for r in rows
        ]
        send_message(token, chat_id, "=== 열린 trade ===\n" + "\n".join(lines))
        return

    # Active conversation flow?
    if conv.awaiting is None:
        send_message(
            token, chat_id,
            "현재 대화 컨텍스트 없음.\n"
            "알람의 버튼을 탭하거나 /close <tid> <price> 사용.",
        )
        return

    # Skip reason
    if conv.awaiting == "skip_reason":
        with connect(db_path) as conn:
            record_skip(conn, alert_id=conv.alert_id, reason=text)
        send_message(token, chat_id, f"✅ {conv.alert_id} 패스 기록됨\n   이유: {text}")
        conv.reset()
        return

    # Custom take steps
    try:
        value = float(text)
    except ValueError:
        send_message(token, chat_id, "숫자만 답장 가능. 예: 81250")
        return

    if conv.awaiting == "entry":
        conv.partial["entry"] = value
        conv.awaiting = "sl"
        send_message(token, chat_id, f"진입가 {value} 저장. SL 가격은?")
        return
    if conv.awaiting == "sl":
        conv.partial["sl"] = value
        conv.awaiting = "tp"
        send_message(token, chat_id, f"SL {value} 저장. TP 가격은?")
        return
    if conv.awaiting == "tp":
        conv.partial["tp"] = value
        conv.awaiting = "size"
        default_hint = f" (기본 ${default_size:.0f}, 0 = skip)" if default_size else " (0 = skip)"
        send_message(token, chat_id, f"TP {value} 저장. 포지션 크기 USD?{default_hint}")
        return
    if conv.awaiting == "size":
        size = None
        if value > 0:
            size = value
        elif default_size and value == -1:  # special: -1 = use default
            size = default_size
        with connect(db_path) as conn:
            tid = record_trade(
                conn, alert_id=conv.alert_id,
                filled_entry=conv.partial["entry"],
                filled_sl=conv.partial["sl"],
                filled_tp=conv.partial["tp"],
                position_size=size,
                notes="taken via telegram (custom)",
            )
        size_str = f" size=${size:.0f}" if size else ""
        send_message(
            token, chat_id,
            f"✅ Trade #{tid} opened\n"
            f"   entry={conv.partial['entry']} SL={conv.partial['sl']} TP={conv.partial['tp']}{size_str}\n"
            f"   종료 시: /close {tid} <exit_price>",
        )
        conv.reset()
        return


# --- Main loop ---
def process_update(
    update: dict, *, token: str, db_path: Path, conv_states: dict[str, ConvState],
    default_size: float | None,
) -> None:
    """Dispatch one update from getUpdates."""
    if "callback_query" in update:
        cb = update["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        callback_id = cb["id"]
        # Acknowledge fast (Telegram requires within ~10 sec)
        try:
            answer_callback(token, callback_id)
        except Exception as e:
            print(f"[listener] answer_callback failed: {e}", file=sys.stderr)

        action, _, alert_id = data.partition(":")
        conv = conv_states.setdefault(str(chat_id), ConvState())
        if action == "take_market":
            handle_take_market(
                token=token, chat_id=chat_id, alert_id=alert_id,
                db_path=db_path, default_size=default_size,
            )
        elif action == "take_custom":
            handle_take_custom_start(
                token=token, chat_id=chat_id, alert_id=alert_id, conv=conv,
            )
        elif action == "skip":
            handle_skip_start(
                token=token, chat_id=chat_id, alert_id=alert_id, conv=conv,
            )
        else:
            send_message(token, chat_id, f"알 수 없는 action: {action}")
        return

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        if not text:
            return
        conv = conv_states.setdefault(str(chat_id), ConvState())
        handle_reply(
            token=token, chat_id=chat_id, text=text,
            conv=conv, db_path=db_path, default_size=default_size,
        )


def run_forever() -> None:
    """Main long-polling loop. Survives transient API errors."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN 환경변수 필요", file=sys.stderr)
        sys.exit(2)

    db_path = Path(os.environ.get(JOURNAL_DB_ENV, str(DEFAULT_DB_PATH)))
    default_size_str = os.environ.get(DEFAULT_POSITION_SIZE_ENV, "").strip()
    default_size: float | None = float(default_size_str) if default_size_str else None

    state = load_state()
    offset = state.get("offset", 0)
    chat_state_raw = state.get("chats", {})
    conv_states: dict[str, ConvState] = {
        cid: ConvState.from_json(d) for cid, d in chat_state_raw.items()
    }

    print(f"[listener] started; offset={offset}, db={db_path}, default_size={default_size}")
    while True:
        try:
            updates = get_updates(token, offset)
        except Exception as e:
            print(f"[listener] poll error: {e}", file=sys.stderr)
            time.sleep(5)
            continue
        for upd in updates:
            try:
                process_update(
                    upd, token=token, db_path=db_path,
                    conv_states=conv_states, default_size=default_size,
                )
            except Exception as e:
                print(f"[listener] process error: {e}", file=sys.stderr)
            offset = upd["update_id"] + 1
        if updates:
            state["offset"] = offset
            state["chats"] = {cid: c.to_json() for cid, c in conv_states.items()}
            save_state(state)


if __name__ == "__main__":
    run_forever()
