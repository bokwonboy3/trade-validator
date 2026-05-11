"""Smoke test the Telegram listener end-to-end without waiting for a real alert.

What it does:
1. Inserts a clearly-fake alert (`SMOKE-TEST-<TS>`) into journal.db so the
   listener's _scanner_alert lookup works for the "take_market" button.
2. Sends a Telegram message with the 3 inline buttons attached, pointing at
   that fake alert ID.
3. Prints expected behavior and how to clean up afterwards.

Prerequisites:
- TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars set (source .tv-env)
- Listener running in another terminal: `.venv/bin/python telegram_listener.py`

Usage:
    .venv/bin/python scripts/smoke_test_listener.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make repo root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.journal_db import connect, insert_alert_idempotent
from output.notify import TelegramChannel
from scan import _alert_buttons


def main() -> int:
    tg = TelegramChannel.from_env()
    if tg is None:
        print(
            "❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수 없음.\n"
            "   source .tv-env  먼저 실행",
            file=sys.stderr,
        )
        return 2

    ts = datetime.now(timezone.utc)
    alert_id = f"SMOKE-TEST-{ts.strftime('%Y%m%d%H%M%S')}"
    fake_record = {
        "alert_id": alert_id,
        "ts": ts.isoformat(),
        "symbol": "TESTUSDT",
        "direction": "long",
        "tier": "confirmed",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "tier1_score": 4,
        "agent_verdict": "ENTER",
        "agent_confidence": 75,
        "agent_downgraded": False,
    }
    db_path = Path(os.environ.get("JOURNAL_DB", "journal.db"))
    with connect(db_path) as conn:
        insert_alert_idempotent(conn, fake_record)
    print(f"📝 Inserted fake alert: {alert_id}")

    body = (
        "🧪 SMOKE TEST — listener 검증용 메시지\n"
        f"Alert ID: {alert_id}\n"
        "Symbol: TESTUSDT LONG (가짜)\n"
        "Entry: 100 / SL: 99 / TP: 103\n\n"
        "버튼 하나 탭해서 listener 응답 확인하세요.\n"
        "끝나면 journal.db에서 SMOKE-TEST-* 행 정리 가능."
    )
    tg.send(body, inline_keyboard=_alert_buttons(alert_id))
    print(f"✅ Sent test message with 3 buttons → chat {tg.chat_id}")
    print()
    print("Expected:")
    print("  📥 진입 (시장가) → '✅ Trade #N opened (scanner 가격 사용)'")
    print("  ✏️ 진입 (가격 입력) → '진입 가격을 답장으로 보내세요'")
    print("  ⏭ 패스 → '패스 이유를 답장으로 보내세요'")
    print()
    print("정리 (smoke 테스트 결과 journal에서 빼고 싶으면):")
    print(f"  sqlite3 {db_path} \"DELETE FROM trades WHERE alert_id LIKE 'SMOKE-TEST-%';\"")
    print(f"  sqlite3 {db_path} \"DELETE FROM skipped WHERE alert_id LIKE 'SMOKE-TEST-%';\"")
    print(f"  sqlite3 {db_path} \"DELETE FROM alerts WHERE id LIKE 'SMOKE-TEST-%';\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
