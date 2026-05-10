from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from alert_state import (
    ALERT_TTL_HOURS,
    AlertRecord,
    AlertState,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_load_missing_file_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    s = AlertState.load(p)
    assert s.alerts == []
    assert s.path == p


def test_record_and_save_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    s = AlertState.load(p)
    s.record("BTCUSDT", "long", 80_000.0)
    s.save()

    body = json.loads(p.read_text())
    assert len(body["alerts"]) == 1
    a = body["alerts"][0]
    assert a["symbol"] == "BTCUSDT"
    assert a["direction"] == "long"
    assert a["sl_swing_price"] == 80_000.0
    assert "alerted_at" in a


def test_already_alerted_within_window(tmp_path):
    p = tmp_path / "state.json"
    s = AlertState.load(p)
    s.record("BTCUSDT", "long", 80_000.0)
    assert s.already_alerted("BTCUSDT", "long", 80_000.0) is True


def test_not_alerted_for_different_symbol(tmp_path):
    s = AlertState(path=tmp_path / "x.json")
    s.record("BTCUSDT", "long", 80_000.0)
    assert s.already_alerted("ETHUSDT", "long", 80_000.0) is False


def test_not_alerted_for_different_direction(tmp_path):
    s = AlertState(path=tmp_path / "x.json")
    s.record("BTCUSDT", "long", 80_000.0)
    assert s.already_alerted("BTCUSDT", "short", 80_000.0) is False


def test_swing_price_tolerance_treats_close_prices_as_same(tmp_path):
    """0.05% difference should still count as same swing."""
    s = AlertState(path=tmp_path / "x.json")
    s.record("BTCUSDT", "long", 80_000.0)
    # 0.05% off — within 0.1% tolerance
    assert s.already_alerted("BTCUSDT", "long", 80_040.0) is True


def test_swing_price_tolerance_rejects_far_prices(tmp_path):
    s = AlertState(path=tmp_path / "x.json")
    s.record("BTCUSDT", "long", 80_000.0)
    # 0.5% off — outside tolerance
    assert s.already_alerted("BTCUSDT", "long", 80_400.0) is False


def test_stale_alerts_pruned_on_load(tmp_path):
    p = tmp_path / "state.json"
    stale = datetime.now(timezone.utc) - timedelta(hours=ALERT_TTL_HOURS + 1)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    body = {
        "alerts": [
            {"symbol": "OLD", "direction": "long", "sl_swing_price": 100.0,
             "alerted_at": _iso(stale)},
            {"symbol": "NEW", "direction": "long", "sl_swing_price": 100.0,
             "alerted_at": _iso(fresh)},
        ]
    }
    p.write_text(json.dumps(body))
    s = AlertState.load(p)
    assert len(s.alerts) == 1
    assert s.alerts[0].symbol == "NEW"


def test_already_alerted_outside_ttl_returns_false(tmp_path):
    p = tmp_path / "state.json"
    s = AlertState(path=p)
    # Manually inject an old record
    s.alerts.append(
        AlertRecord(
            symbol="BTCUSDT",
            direction="long",
            sl_swing_price=80_000.0,
            alerted_at=_iso(datetime.now(timezone.utc) - timedelta(hours=ALERT_TTL_HOURS + 1)),
        )
    )
    assert s.already_alerted("BTCUSDT", "long", 80_000.0) is False


def test_save_uses_atomic_write(tmp_path):
    p = tmp_path / "state.json"
    s = AlertState(path=p)
    s.record("BTCUSDT", "long", 80_000.0)
    s.save()
    # No leftover .tmp file
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []
    assert p.exists()


def test_record_then_load_persists(tmp_path):
    p = tmp_path / "state.json"
    s1 = AlertState(path=p)
    s1.record("BTCUSDT", "long", 80_000.0)
    s1.save()

    s2 = AlertState.load(p)
    assert len(s2.alerts) == 1
    assert s2.alerts[0].symbol == "BTCUSDT"
