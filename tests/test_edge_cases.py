"""Edge case tests — empty/minimal data, NaN propagation, boundary handling.

These guard against silent breakage when upstream data is unusual:
- new symbols with thin history
- API returning fewer rows than requested
- Synthetic test data that doesn't span enough candles
"""
from __future__ import annotations

import pandas as pd

from analysis.candles import is_hammer, is_shooting_star, is_volume_spike
from analysis.indicators import add_ma, latest_ma
from analysis.layers import (
    layer_1_trend,
    layer_2_setup_zone,
    layer_3_rejection,
    layer_4_sl_structure,
)
from analysis.levels import find_swings
from output.formatter import ValidationReport, format_report


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(
        {"openTime": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "closeTime": []}
    )


# --- Indicators ---
def test_add_ma_with_insufficient_rows_returns_nan():
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
    out = add_ma(df, [25])
    assert pd.isna(out["ma_25"].iloc[-1])
    assert pd.isna(latest_ma(out, 25))


def test_add_ma_empty_df():
    df = pd.DataFrame({"close": []})
    out = add_ma(df, [25, 99])
    assert "ma_25" in out.columns and "ma_99" in out.columns
    assert len(out) == 0


# --- Levels ---
def test_find_swings_empty_df():
    assert find_swings(_empty_df(), n=3) == []


def test_find_swings_too_few_rows_returns_empty():
    # n=5 needs at least 11 rows (5 left + center + 5 right) for any candidate
    df = pd.DataFrame(
        {"high": [1, 2, 3, 4, 5], "low": [0, 0, 0, 0, 0], "close": [1, 2, 3, 4, 5], "open": [0, 0, 0, 0, 0], "volume": [1] * 5}
    )
    assert find_swings(df, n=5) == []


def test_find_swings_monotonic_no_swings():
    n = 30
    df = pd.DataFrame(
        {
            "high": list(range(n)),
            "low": [-1] * n,
            "close": list(range(n)),
            "open": list(range(n)),
            "volume": [1] * n,
        }
    )
    # Strictly ascending highs → no swing high (right side never lower).
    swings = find_swings(df, n=3)
    assert all(s.kind == "low" for s in swings) or swings == []


# --- Layer 1 with NaN MA ---
def test_layer_1_with_nan_ma_fails_gracefully():
    df = pd.DataFrame({"close": [100.0, 101.0]})  # too few for MA25
    out = add_ma(df, [25, 99])
    res_long = layer_1_trend(out, "long")
    res_short = layer_1_trend(out, "short")
    # NaN > NaN → False, NaN < NaN → False → both fail (correct conservative behavior)
    assert res_long.status == "fail"
    assert res_short.status == "fail"


# --- Layer 2 with no levels ---
def test_layer_2_with_no_levels_returns_fail_with_reason():
    """When there's no swing AND no MA, Layer 2 must not crash."""
    # Tiny df: too small for swings (n=5 needs 11 rows) AND too small for MA25/99
    df = pd.DataFrame(
        {
            "openTime": [i * 3_600_000 for i in range(3)],
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
            "volume": [1.0, 1.0, 1.0],
            "closeTime": [(i + 1) * 3_600_000 - 1 for i in range(3)],
        }
    )
    df = add_ma(df, [25, 99])
    res = layer_2_setup_zone(df, entry=100.0)
    assert res.status == "fail"
    assert res.detail["reason"] == "no_levels_found"
    assert res.detail["all_levels"] == []


def test_formatter_layer_2_no_levels_emits_clean_message():
    """No 'inf%' or empty label garbage in the output."""
    from analysis.layers import LayerResult

    bad_l2 = LayerResult(
        score=0,
        status="fail",
        detail={
            "closest_label": "(none)",
            "closest_price": 0.0,
            "distance_pct": 0.0,
            "all_levels": [],
            "reason": "no_levels_found",
        },
    )
    rep = ValidationReport(
        symbol="BTCUSDT",
        direction="long",
        entry=100.0,
        sl=99.0,
        tp=103.0,
        layer_1=LayerResult(0, "fail", {"ma25": 100.0, "ma99": 100.0}),
        layer_2=bad_l2,
        layer_3=LayerResult(0, "pending", {"reason": "no_touch_in_history"}),
        layer_4=LayerResult(0, "fail", {"passed_swing_price": None, "closest_swing_price": None}),
        layer_5=LayerResult(1, "pass", {"rr": 3.0, "reward": 3.0, "risk": 1.0, "min_rr": 3.0}),
    )
    out = format_report(rep)
    assert "데이터 부족" in out
    assert "inf" not in out
    assert "(none)" not in out  # the user-visible line shouldn't show the placeholder label


# --- Layer 3 edge cases ---
def test_layer_3_with_empty_15m_when_touched():
    """Touch found in 1H, but no 15m candles match the window → pending."""
    # Single 1H candle whose [low, high] covers entry 100
    df_1h = pd.DataFrame(
        {
            "openTime": [1_000_000_000_000],
            "open": [100.0],
            "high": [100.5],
            "low": [99.5],
            "close": [100.0],
            "volume": [1.0],
            "closeTime": [1_000_000_000_000 + 3_600_000 - 1],
        }
    )
    # 15m frame whose openTimes are nowhere near the 1H window
    df_15m = pd.DataFrame(
        {
            "openTime": [2_000_000_000_000 + i * 900_000 for i in range(10)],
            "open": [100.0] * 10,
            "high": [100.5] * 10,
            "low": [99.5] * 10,
            "close": [100.0] * 10,
            "volume": [1.0] * 10,
            "closeTime": [2_000_000_000_000 + (i + 1) * 900_000 - 1 for i in range(10)],
        }
    )
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "pending"
    assert "15m" in res.detail.get("reason", "") or res.detail.get("reason") == "15m_data_missing_for_touch_window"


def test_layer_3_with_empty_1h_returns_pending():
    df_1h = _empty_df()
    df_15m = _empty_df()
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "pending"
    assert res.detail["reason"] == "no_touch_in_history"


# --- Layer 4 edge cases ---
def test_layer_4_with_no_swings_fails_with_no_candidates():
    """When there are no swings of the right kind, Layer 4 returns fail with None price."""
    # Very flat data — no swings at all
    n = 30
    df = pd.DataFrame(
        {
            "openTime": [i * 3_600_000 for i in range(n)],
            "open": [100.0] * n,
            "high": [100.5] * n,
            "low": [99.5] * n,
            "close": [100.0] * n,
            "volume": [1.0] * n,
            "closeTime": [(i + 1) * 3_600_000 - 1 for i in range(n)],
        }
    )
    res = layer_4_sl_structure(df, sl=99.0, direction="long", n_swing=5)
    assert res.status == "fail"
    assert res.detail["passed_swing_price"] is None


# --- Candle pattern boundary ---
def test_candle_with_zero_range_not_a_pattern():
    """high == low == open == close: zero-range candle (perfect doji-like). Reject."""
    candle = {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}
    assert not is_hammer(candle)
    assert not is_shooting_star(candle)


def test_volume_spike_with_negative_avg_returns_false():
    # Defensive: shouldn't happen with real data, but ensure no division-by-near-zero
    assert is_volume_spike(volume=200, prev_volumes=[-5, -5]) is False
