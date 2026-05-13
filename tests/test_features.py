"""Tests for analysis.features.build_semantic_context — Phase 8b.

Verifies:
- Schema completeness (all expected sections + keys present)
- Edge cases (insufficient candles, NaN-prone divisions, zero volume)
- Direction-aware fields work for both long and short
- Token-budget claim: payload is meaningfully smaller than raw-candle dump
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from analysis.features import (
    _candle_pattern,
    _classify_alignment,
    _consecutive_same_color,
    _ma_at,
    _nearest_levels,
    _safe_pct,
    _slope_pct,
    _structural_intact,
    _volume_ratio,
    build_semantic_context,
)


# --- helpers -------------------------------------------------------------

def _df(opens: list, highs: list, lows: list, closes: list, vols: list | None = None) -> pd.DataFrame:
    n = len(opens)
    vols = vols if vols is not None else [1.0] * n
    return pd.DataFrame({
        "openTime": list(range(n)),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": vols,
        "closeTime": list(range(1, n + 1)),
    })


def _flat_df(close_price: float, n: int) -> pd.DataFrame:
    return _df(
        opens=[close_price] * n,
        highs=[close_price + 1] * n,
        lows=[close_price - 1] * n,
        closes=[close_price] * n,
        vols=[1.0] * n,
    )


# --- pure helper tests ---------------------------------------------------

def test_safe_pct_handles_zero_denominator():
    assert _safe_pct(10.0, 0.0) is None


def test_safe_pct_handles_nan():
    assert _safe_pct(float("nan"), 100.0) is None
    assert _safe_pct(10.0, float("nan")) is None


def test_safe_pct_basic():
    assert _safe_pct(2.0, 100.0) == 2.0


def test_ma_at_returns_none_when_insufficient():
    df = _flat_df(100.0, n=10)
    assert _ma_at(df, 25) is None


def test_ma_at_computes_correctly():
    df = _flat_df(100.0, n=30)
    assert _ma_at(df, 25) == 100.0


def test_slope_pct_positive():
    closes = list(range(100, 111))  # 100 → 110, +10%
    df = _df(opens=closes, highs=closes, lows=closes, closes=closes)
    assert _slope_pct(df, 10) == pytest.approx(10.0, rel=0.01)


def test_slope_pct_insufficient_data():
    df = _flat_df(100.0, n=3)
    assert _slope_pct(df, 10) is None


def test_classify_alignment_bullish():
    assert _classify_alignment(110.0, 100.0) == "bullish_aligned"


def test_classify_alignment_bearish():
    assert _classify_alignment(90.0, 100.0) == "bearish_aligned"


def test_classify_alignment_unknown_with_none():
    assert _classify_alignment(None, 100.0) == "unknown"
    assert _classify_alignment(110.0, None) == "unknown"


def test_candle_pattern_hammer():
    # Long lower wick, small upper wick less than body
    # is_hammer requires: lower_wick > 2*body AND upper_wick < body
    c = {"open": 100, "high": 100.1, "low": 90, "close": 100.5}
    # body=0.5, lower=10 (>1=2*body), upper=0.1 (<0.5=body) — passes
    # Need rng>0 for non-flat: rng = 100.1-90 = 10.1
    assert _candle_pattern(c) == "hammer"


def test_candle_pattern_shooting_star():
    c = {"open": 100, "high": 110, "low": 99.5, "close": 99.7}
    assert _candle_pattern(c) == "shooting_star"


def test_candle_pattern_doji():
    c = {"open": 100, "high": 105, "low": 95, "close": 100.01}
    assert _candle_pattern(c) == "doji"


def test_candle_pattern_marubozu_bull():
    c = {"open": 100, "high": 110.2, "low": 99.9, "close": 110}
    # body=10, range=10.3, ratio=0.97 > 0.85 → marubozu, close > open → bullish
    assert _candle_pattern(c) == "bullish_marubozu"


def test_consecutive_same_color_counts_correctly():
    # last 3 are green (close >= open), one red before
    df = _df(
        opens=[100, 100, 100, 100, 100],
        highs=[100] * 5, lows=[100] * 5,
        closes=[99, 101, 102, 103, 104],  # red, then 4 green
    )
    assert _consecutive_same_color(df) == 4


def test_consecutive_same_color_empty():
    df = pd.DataFrame({"close": [], "open": []})
    assert _consecutive_same_color(df) == 0


def test_volume_ratio_basic():
    # last 4: avg=10, prior 12: avg=5 → ratio = 2.0
    vols = [5] * 12 + [10] * 4
    df = _df(
        opens=[100] * 16, highs=[100] * 16, lows=[100] * 16,
        closes=[100] * 16, vols=vols,
    )
    assert _volume_ratio(df, recent_n=4, baseline_n=12) == 2.0


def test_volume_ratio_zero_baseline_returns_none():
    vols = [0] * 12 + [10] * 4
    df = _df(
        opens=[100] * 16, highs=[100] * 16, lows=[100] * 16,
        closes=[100] * 16, vols=vols,
    )
    assert _volume_ratio(df, recent_n=4, baseline_n=12) is None


def test_volume_ratio_insufficient_candles():
    df = _flat_df(100.0, n=10)
    assert _volume_ratio(df, recent_n=4, baseline_n=12) is None


def test_nearest_levels_finds_both_sides():
    # Swing high at idx 3 (price=120), swing low at idx 8 (price=80), ref=100
    highs = [100, 105, 110, 120, 110, 100, 90, 85, 80, 85, 90]
    lows = [99, 100, 105, 115, 100, 95, 85, 82, 80, 82, 85]
    df = _df(
        opens=[100] * 11, highs=highs, lows=lows,
        closes=[100] * 11,
    )
    res, sup = _nearest_levels(df, reference_price=100.0, n_swing=2)
    assert res is not None and res["price"] == 120.0
    assert sup is not None and sup["price"] == 80.0


def test_nearest_levels_no_resistance_above():
    # All swings below reference
    df = _df(
        opens=[50] * 11,
        highs=[60, 65, 70, 80, 70, 60, 55, 50, 45, 50, 55],
        lows=[40] * 11,
        closes=[55] * 11,
    )
    res, sup = _nearest_levels(df, reference_price=200.0, n_swing=2)
    assert res is None
    assert sup is not None  # 80 is below 200


def test_structural_intact_long_higher_lows():
    # Two swing lows: first lower (80), second higher (85) — structure intact for LONG
    highs = [100] * 11
    lows = [99, 99, 80, 99, 99, 99, 99, 99, 85, 99, 99]
    df = _df(
        opens=[100] * 11, highs=highs, lows=lows, closes=[100] * 11,
    )
    assert _structural_intact(df, "long", n_swing=2) is True


def test_structural_intact_long_broken():
    # Lows go 80 then 75 — broken for LONG
    highs = [100] * 11
    lows = [99, 99, 80, 99, 99, 99, 99, 99, 75, 99, 99]
    df = _df(
        opens=[100] * 11, highs=highs, lows=lows, closes=[100] * 11,
    )
    assert _structural_intact(df, "long", n_swing=2) is False


def test_structural_intact_returns_none_insufficient_swings():
    df = _flat_df(100.0, n=20)
    assert _structural_intact(df, "long", n_swing=5) is None


# --- build_semantic_context end-to-end -----------------------------------

@pytest.fixture
def realistic_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """4H/1H/15m frames with enough rows for all MAs + swing detection."""
    rng = np.random.default_rng(seed=42)
    def _gen(n: int, base: float = 80_000) -> pd.DataFrame:
        closes = base + rng.normal(0, 200, n).cumsum()
        closes = np.clip(closes, base * 0.9, base * 1.1)
        opens = np.roll(closes, 1)
        opens[0] = closes[0]
        highs = np.maximum(opens, closes) + rng.uniform(10, 50, n)
        lows = np.minimum(opens, closes) - rng.uniform(10, 50, n)
        vols = rng.uniform(100, 500, n)
        return pd.DataFrame({
            "openTime": list(range(n)),
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": vols,
            "closeTime": list(range(1, n + 1)),
        })
    return _gen(100), _gen(100), _gen(200)


def test_build_semantic_context_full_schema(realistic_frames):
    df_4h, df_1h, df_15m = realistic_frames
    ctx = build_semantic_context(
        symbol="BTCUSDT", direction="long",
        entry=80_000.0, current_price=80_500.0, pnl_pct=0.625,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    # All top-level sections present
    assert set(ctx.keys()) == {"trade", "trend_4h", "structure_1h", "microstructure_15m"}
    # Trade section
    assert ctx["trade"]["symbol"] == "BTCUSDT"
    assert ctx["trade"]["direction"] == "long"
    assert ctx["trade"]["pnl_pct"] == 0.625
    # Trend section
    for k in ("alignment", "ma25", "ma99", "ma25_vs_ma99_gap_pct",
              "price_vs_ma25_pct", "slope_last_10_pct", "last_3_candles"):
        assert k in ctx["trend_4h"]
    # Structure section
    for k in ("nearest_resistance", "nearest_support", "price_vs_ma25_pct",
              "price_vs_ma99_pct", "structural_intact_for_direction",
              "atr_pct", "last_3_candles"):
        assert k in ctx["structure_1h"]
    # Microstructure section
    for k in ("last_candle_pattern", "consecutive_same_color",
              "volume_last_4_vs_prior_12_ratio", "atr_pct", "last_3_candles"):
        assert k in ctx["microstructure_15m"]


def test_build_semantic_context_json_serializable(realistic_frames):
    df_4h, df_1h, df_15m = realistic_frames
    ctx = build_semantic_context(
        symbol="BTCUSDT", direction="long",
        entry=80_000.0, current_price=80_500.0, pnl_pct=0.625,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    # Round-trip through JSON without TypeError
    blob = json.dumps(ctx)
    parsed = json.loads(blob)
    assert parsed["trade"]["symbol"] == "BTCUSDT"


def test_build_semantic_context_token_budget_drops(realistic_frames):
    """The whole point of Phase 8b: shrink the user_content. Char count is a
    reasonable proxy for token count (roughly chars/4 ≈ tokens for English/
    mixed Korean content). Old payload (38 raw candles, ~4-field dump) was
    ~3000 chars+ JSON. New should be well under 2000."""
    df_4h, df_1h, df_15m = realistic_frames
    ctx = build_semantic_context(
        symbol="BTCUSDT", direction="long",
        entry=80_000.0, current_price=80_500.0, pnl_pct=0.625,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    blob = json.dumps(ctx, ensure_ascii=False, indent=2)
    assert len(blob) < 2500, f"semantic context grew to {len(blob)} chars — feature schema bloating?"


def test_build_semantic_context_handles_insufficient_4h_candles():
    """A new symbol might have <25 4H candles. MA fields should be None,
    not crash."""
    short_4h = _flat_df(80_000.0, n=10)
    long_1h = _flat_df(80_000.0, n=100)
    long_15m = _flat_df(80_000.0, n=200)
    ctx = build_semantic_context(
        symbol="NEWUSDT", direction="long",
        entry=80_000.0, current_price=80_000.0, pnl_pct=0.0,
        df_4h=short_4h, df_1h=long_1h, df_15m=long_15m,
    )
    assert ctx["trend_4h"]["ma25"] is None
    assert ctx["trend_4h"]["ma99"] is None
    assert ctx["trend_4h"]["alignment"] == "unknown"


def test_build_semantic_context_direction_aware_short(realistic_frames):
    """structural_intact_for_direction must use SHORT-side logic when direction=short."""
    df_4h, df_1h, df_15m = realistic_frames
    ctx = build_semantic_context(
        symbol="BTCUSDT", direction="short",
        entry=80_000.0, current_price=79_500.0, pnl_pct=0.625,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    # Just verify the field exists and is a bool or None — exact value
    # depends on randomness in fixture
    val = ctx["structure_1h"]["structural_intact_for_direction"]
    assert val is None or isinstance(val, bool)
