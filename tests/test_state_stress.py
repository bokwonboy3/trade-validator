"""Stress + concurrency safety for AlertState.

Cron 5-min 실행이 24h만 돌아도 288번. 1주면 2000+. 그 누적 write가 state
파일을 깨뜨리지 않는지, prune 로직이 누적 항목을 관리하는지 검증.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alert_state import (
    ALERT_TTL_HOURS,
    AlertRecord,
    AlertState,
)


def test_500_rapid_record_save_cycles(tmp_path: Path):
    """500 load → record → save 사이클 후 state JSON이 깨지지 않음."""
    p = tmp_path / "state.json"
    for i in range(500):
        s = AlertState.load(p)
        s.record(f"SYM{i % 10}", "long", 100.0 + i * 0.001)
        s.save()

    # JSON validity
    body = json.loads(p.read_text())
    assert "alerts" in body
    # 모든 항목이 alerted_at 형식 갖춤
    for a in body["alerts"]:
        datetime.fromisoformat(a["alerted_at"])  # 파싱 안 되면 raise


def test_stale_records_pruned_on_load(tmp_path: Path):
    """과거 alerted_at을 가진 항목은 load 시 자동 prune되어 누적되지 않음."""
    p = tmp_path / "state.json"
    s = AlertState(path=p)
    # 25h 전 (TTL 24h 초과) 항목 한 개 + 1h 전 항목 한 개
    stale = datetime.now(timezone.utc) - timedelta(hours=ALERT_TTL_HOURS + 1)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    s.alerts.append(AlertRecord("OLD", "long", 100.0, stale.isoformat()))
    s.alerts.append(AlertRecord("NEW", "long", 200.0, fresh.isoformat()))
    s.save()

    # 다시 로드하면 OLD 자동 drop
    s2 = AlertState.load(p)
    assert len(s2.alerts) == 1
    assert s2.alerts[0].symbol == "NEW"


def test_atomic_write_no_partial_file(tmp_path: Path):
    """save() 도중 process kill 시뮬레이션은 어렵지만, .tmp 잔존 없음 확인."""
    p = tmp_path / "state.json"
    s = AlertState(path=p)
    s.record("BTCUSDT", "long", 80_000)
    s.save()
    leftover_tmp = list(tmp_path.glob("*.tmp"))
    assert leftover_tmp == [], f"unexpected tmp files: {leftover_tmp}"


def test_many_symbols_dedup_via_signature(tmp_path: Path):
    """100개 심볼 × 양방향이 모두 distinct signature로 기록되고 dedup 가능."""
    p = tmp_path / "state.json"
    s = AlertState(path=p)
    for i in range(100):
        s.record(f"SYM{i}", "long", 100.0 + i)
        s.record(f"SYM{i}", "short", 200.0 + i)
    s.save()

    # 200개 모두 기록됨
    s2 = AlertState.load(p)
    assert len(s2.alerts) == 200

    # 같은 signature 검사 시 매칭
    assert s2.already_alerted("SYM50", "long", 150.0)
    assert s2.already_alerted("SYM50", "short", 250.0)
    # 다른 signature는 매칭 안 됨
    assert not s2.already_alerted("SYM50", "long", 250.0)  # 다른 price
    assert not s2.already_alerted("SYM999", "long", 150.0)  # 다른 symbol
