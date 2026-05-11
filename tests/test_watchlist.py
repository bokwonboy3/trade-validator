"""Tests for watchlist — approaching-setup detection and idempotency."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from analysis.layers import LayerResult, SetupEvaluation
from analysis.scanner_logic import SynthesizedSetup
from watchlist import (
    WATCH_DISTANCE_MAX,
    WATCH_DISTANCE_MIN,
    WatchCandidate,
    WatchlistState,
    filter_unalerted,
    find_watch_candidates,
    format_watch_message,
    watch_key,
)


@dataclass
class _FakeScanResult:
    """Mimics the ScanResult attributes find_watch_candidates uses."""
    symbol: str
    setup: SynthesizedSetup | None
    evaluation: SetupEvaluation | None


def _eval(total_score: int, *, dist_pct: float, closest_price: float) -> SetupEvaluation:
    """Build a SetupEvaluation where total_score is what we set, layer_2
    carries the closest_price/distance_pct we want to test against."""
    l1 = LayerResult(score=1 if total_score >= 1 else 0, status="pass")
    l2 = LayerResult(
        score=1 if dist_pct <= 0.003 else 0,
        status="pass" if dist_pct <= 0.003 else "fail",
        detail={
            "closest_label": "swing_low",
            "closest_price": closest_price,
            "distance_pct": dist_pct,
        },
    )
    others = [LayerResult(score=1, status="pass") for _ in range(3)]
    # Total score override — keep layer_2 score honest, fill rest to hit target
    remaining = total_score - l1.score - l2.score
    fillers: list[LayerResult] = []
    for _ in range(3):
        if remaining > 0:
            fillers.append(LayerResult(score=1, status="pass"))
            remaining -= 1
        else:
            fillers.append(LayerResult(score=0, status="fail"))
    return SetupEvaluation(
        layer_1=l1, layer_2=l2,
        layer_3=fillers[0], layer_4=fillers[1], layer_5=fillers[2],
    )


def _setup(direction: str = "long", entry: float = 80_000.0) -> SynthesizedSetup:
    return SynthesizedSetup(
        direction=direction, entry=entry,
        sl=entry * 0.99, tp=entry * 1.03, sl_swing_price=entry * 0.992,
    )


# ---------- find_watch_candidates ----------

def test_find_picks_up_approaching_setup():
    # score 3/5, distance 0.6% — squarely in [0.3%, 1.5%]
    r = _FakeScanResult(
        symbol="BTCUSDT",
        setup=_setup(),
        evaluation=_eval(3, dist_pct=0.006, closest_price=79_520.0),
    )
    cands = find_watch_candidates([r])
    assert len(cands) == 1
    assert cands[0].symbol == "BTCUSDT"
    assert cands[0].target_price == 79_520.0
    assert cands[0].distance_pct == pytest.approx(0.006)


def test_find_skips_confirmed_setup():
    # score 5/5 (≥ threshold) — would already dispatch as confirmed, not watchlist
    r = _FakeScanResult(
        symbol="BTCUSDT",
        setup=_setup(),
        evaluation=_eval(5, dist_pct=0.001, closest_price=80_000.0),
    )
    assert find_watch_candidates([r]) == []


def test_find_skips_too_close():
    # distance < 0.3% → Layer 2 would already pass, no need to watch
    r = _FakeScanResult(
        symbol="BTCUSDT",
        setup=_setup(),
        evaluation=_eval(2, dist_pct=0.001, closest_price=80_000.0),
    )
    assert find_watch_candidates([r]) == []


def test_find_skips_too_far():
    # distance > 1.5% → too far away to be "approaching"
    r = _FakeScanResult(
        symbol="BTCUSDT",
        setup=_setup(),
        evaluation=_eval(2, dist_pct=0.05, closest_price=84_000.0),
    )
    assert find_watch_candidates([r]) == []


def test_find_skips_error_results():
    r = _FakeScanResult(symbol="BTCUSDT", setup=None, evaluation=None)
    assert find_watch_candidates([r]) == []


def test_find_boundary_distance_min_inclusive():
    r = _FakeScanResult(
        symbol="BTCUSDT",
        setup=_setup(),
        evaluation=_eval(2, dist_pct=WATCH_DISTANCE_MIN, closest_price=79_760.0),
    )
    assert len(find_watch_candidates([r])) == 1


def test_find_boundary_distance_max_inclusive():
    r = _FakeScanResult(
        symbol="BTCUSDT",
        setup=_setup(),
        evaluation=_eval(2, dist_pct=WATCH_DISTANCE_MAX, closest_price=78_800.0),
    )
    assert len(find_watch_candidates([r])) == 1


# ---------- watch_key ----------

def test_watch_key_stable_across_micro_fluctuations():
    # 81537.1 and 81540.0 should round to the same key (4 sig figs)
    k1 = watch_key("BTCUSDT", "long", 81_537.1)
    k2 = watch_key("BTCUSDT", "long", 81_540.0)
    assert k1 == k2


def test_watch_key_differs_for_different_levels():
    k1 = watch_key("BTCUSDT", "long", 80_000.0)
    k2 = watch_key("BTCUSDT", "long", 82_000.0)
    assert k1 != k2


def test_watch_key_includes_direction():
    k_long = watch_key("BTCUSDT", "long", 80_000.0)
    k_short = watch_key("BTCUSDT", "short", 80_000.0)
    assert k_long != k_short


# ---------- WatchlistState ----------

def test_state_load_missing_file(tmp_path):
    s = WatchlistState.load(tmp_path / "nope.json")
    assert s.alerted == {}


def test_state_record_and_save(tmp_path):
    p = tmp_path / "watch.json"
    s = WatchlistState(path=p)
    s.record("BTCUSDT:long:80000")
    s.save()
    raw = json.loads(p.read_text())
    assert "BTCUSDT:long:80000" in raw["alerted"]


def test_state_load_prunes_stale(tmp_path):
    from datetime import datetime, timedelta, timezone
    p = tmp_path / "watch.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps({"alerted": {"old:k": old, "fresh:k": fresh}}))
    s = WatchlistState.load(p)
    assert "old:k" not in s.alerted
    assert "fresh:k" in s.alerted


def test_filter_unalerted_drops_already_seen(tmp_path):
    s = WatchlistState(path=tmp_path / "watch.json")
    c = WatchCandidate(
        symbol="BTCUSDT", direction="long", target_price=80_000.0,
        target_label="swing_low", current_entry=80_500.0, distance_pct=0.006,
    )
    s.record(watch_key(c.symbol, c.direction, c.target_price))
    assert filter_unalerted([c], s) == []


def test_filter_unalerted_keeps_new():
    s = WatchlistState()
    c = WatchCandidate(
        symbol="BTCUSDT", direction="long", target_price=80_000.0,
        target_label="swing_low", current_entry=80_500.0, distance_pct=0.006,
    )
    assert filter_unalerted([c], s) == [c]


# ---------- format_watch_message ----------

def test_format_long_direction_words():
    # Target below entry → "내려와야"
    c = WatchCandidate(
        symbol="BTCUSDT", direction="long", target_price=80_000.0,
        target_label="swing_low", current_entry=80_500.0, distance_pct=0.006,
    )
    msg = format_watch_message(c)
    assert "BTCUSDT" in msg
    assert "내려와야" in msg
    assert "swing_low" in msg


def test_format_short_target_above_entry():
    # Target above entry → "올라가야"
    c = WatchCandidate(
        symbol="BTCUSDT", direction="short", target_price=82_000.0,
        target_label="swing_high", current_entry=81_500.0, distance_pct=0.006,
    )
    msg = format_watch_message(c)
    assert "올라가야" in msg
