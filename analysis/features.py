"""Semantic feature engineering for LLM prompts (Phase 8b).

Replaces raw OHLCV candle dumps with pre-computed indicators. The motivation:

- LLMs are bad at tabular numerical reasoning — they re-derive MAs, slopes,
  S/R levels in their head and frequently make arithmetic errors.
- Python computes these deterministically in microseconds.
- The user_content shrinks ~90% (5K tokens of raw candles → ~400 tokens of
  features), which is the dominant cost item per call.

``build_semantic_context`` is the public entry point. It assumes:
- df_4h / df_1h / df_15m are already CLOSED-CANDLE frames (matching
  the rest of the codebase — see data.binance.fetch_klines defaults).
- All numeric fields are rounded for token efficiency.
- Missing data (NaN MA, no swings, etc.) surfaces as ``None`` so the LLM
  can reason about ambiguity rather than getting NaN gibberish.

Direction-aware fields ("structural_break_against_position", etc.) take
the trade direction into account so the LLM gets a directional read,
not just neutral facts.
"""
from __future__ import annotations

from typing import Any, Literal, Mapping

import pandas as pd

from analysis.candles import is_hammer, is_shooting_star
from analysis.indicators import atr
from analysis.levels import find_swings

Direction = Literal["long", "short"]


# --- helpers -------------------------------------------------------------

def _safe_pct(numerator: float, denominator: float) -> float | None:
    """Percent change with NaN/zero guard. Returns None if denominator is
    zero or either operand is NaN."""
    if denominator == 0 or pd.isna(numerator) or pd.isna(denominator):
        return None
    return round(numerator / denominator * 100, 3)


def _ma_at(df: pd.DataFrame, period: int) -> float | None:
    if len(df) < period:
        return None
    val = df["close"].rolling(period).mean().iloc[-1]
    return None if pd.isna(val) else round(float(val), 4)


def _slope_pct(df: pd.DataFrame, n: int) -> float | None:
    """Percent change of close over the last n candles (first → last)."""
    if len(df) < n + 1:
        return None
    start = float(df["close"].iloc[-(n + 1)])
    end = float(df["close"].iloc[-1])
    return _safe_pct(end - start, start)


def _classify_alignment(ma25: float | None, ma99: float | None) -> str:
    """Trend alignment label from MA stack."""
    if ma25 is None or ma99 is None:
        return "unknown"
    if ma25 > ma99:
        return "bullish_aligned"
    if ma25 < ma99:
        return "bearish_aligned"
    return "neutral"


def _candle_pattern(candle: Mapping[str, float]) -> str:
    """One-word pattern for a single candle. Direction-agnostic."""
    if is_hammer(candle):
        return "hammer"
    if is_shooting_star(candle):
        return "shooting_star"
    body = abs(candle["close"] - candle["open"])
    rng = candle["high"] - candle["low"]
    if rng == 0:
        return "flat"
    body_ratio = body / rng
    if body_ratio < 0.1:
        return "doji"
    if body_ratio > 0.85:
        return "bullish_marubozu" if candle["close"] > candle["open"] else "bearish_marubozu"
    return "neutral"


def _consecutive_same_color(df: pd.DataFrame) -> int:
    """Count of consecutive same-color candles at the end of the frame."""
    if len(df) == 0:
        return 0
    closes = df["close"].to_numpy()
    opens = df["open"].to_numpy()
    last_color = "green" if closes[-1] >= opens[-1] else "red"
    count = 0
    for i in range(len(df) - 1, -1, -1):
        color = "green" if closes[i] >= opens[i] else "red"
        if color != last_color:
            break
        count += 1
    return count


def _volume_ratio(df: pd.DataFrame, recent_n: int, baseline_n: int) -> float | None:
    """Mean volume of last recent_n candles ÷ mean of the baseline_n before that."""
    needed = recent_n + baseline_n
    if len(df) < needed:
        return None
    recent_avg = float(df["volume"].iloc[-recent_n:].mean())
    baseline_avg = float(df["volume"].iloc[-needed:-recent_n].mean())
    if baseline_avg <= 0:
        return None
    return round(recent_avg / baseline_avg, 3)


def _nearest_levels(
    df: pd.DataFrame, reference_price: float, n_swing: int = 5,
) -> tuple[dict | None, dict | None]:
    """Closest swing above/below the reference. Each side returns dict
    {label, price, distance_pct} or None if no qualifying swing.

    Distance is signed from the reference: positive for resistance (above),
    negative for support (below).
    """
    swings = find_swings(df, n=n_swing)
    if not swings:
        return None, None

    above = [s for s in swings if s.price > reference_price]
    below = [s for s in swings if s.price < reference_price]

    nearest_res = min(above, key=lambda s: s.price - reference_price) if above else None
    nearest_sup = max(below, key=lambda s: s.price) if below else None

    def _pack(s) -> dict:
        return {
            "label": f"swing_{s.kind}",
            "price": round(s.price, 4),
            "distance_pct": _safe_pct(s.price - reference_price, reference_price),
        }

    return (
        _pack(nearest_res) if nearest_res else None,
        _pack(nearest_sup) if nearest_sup else None,
    )


def _structural_intact(
    df: pd.DataFrame, direction: Direction, n_swing: int = 5,
) -> bool | None:
    """LONG: are the last two swing lows monotonically higher (higher_lows)?
    SHORT: are the last two swing highs monotonically lower (lower_highs)?

    None when not enough swings to evaluate.
    """
    swings = find_swings(df, n=n_swing)
    if direction == "long":
        lows = [s for s in swings if s.kind == "low"]
        if len(lows) < 2:
            return None
        return lows[-1].price > lows[-2].price
    else:
        highs = [s for s in swings if s.kind == "high"]
        if len(highs) < 2:
            return None
        return highs[-1].price < highs[-2].price


def _compact_candle(c: Mapping[str, Any]) -> dict:
    """Tiny verification snapshot — one candle as 5 fields, no openTime."""
    return {
        "o": round(float(c["open"]), 4),
        "h": round(float(c["high"]), 4),
        "l": round(float(c["low"]), 4),
        "c": round(float(c["close"]), 4),
        "v": round(float(c["volume"]), 2),
    }


# --- public entry --------------------------------------------------------

def build_semantic_context(
    *,
    symbol: str,
    direction: Direction,
    entry: float,
    current_price: float,
    pnl_pct: float,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> dict:
    """Build the LLM-facing feature dict for one trade snapshot.

    ~15-20 features split into trade / trend_4h / structure_1h /
    microstructure_15m sections, plus 3 raw candles per timeframe as a
    light verification anchor (LLM can sanity-check derived values).

    Output is JSON-serializable and intentionally compact (rounded floats,
    no timestamps in the verification candles).
    """
    # 4H trend
    ma25_4h = _ma_at(df_4h, 25)
    ma99_4h = _ma_at(df_4h, 99)
    last_close_4h = float(df_4h["close"].iloc[-1]) if len(df_4h) else None
    trend_4h = {
        "alignment": _classify_alignment(ma25_4h, ma99_4h),
        "ma25": ma25_4h,
        "ma99": ma99_4h,
        "ma25_vs_ma99_gap_pct": (
            _safe_pct(ma25_4h - ma99_4h, max(abs(ma25_4h), abs(ma99_4h)))
            if (ma25_4h is not None and ma99_4h is not None) else None
        ),
        "price_vs_ma25_pct": (
            _safe_pct(last_close_4h - ma25_4h, ma25_4h)
            if (last_close_4h is not None and ma25_4h is not None) else None
        ),
        "slope_last_10_pct": _slope_pct(df_4h, 10),
        "last_3_candles": [_compact_candle(df_4h.iloc[i]) for i in range(-3, 0)] if len(df_4h) >= 3 else [],
    }

    # 1H structure
    ma25_1h = _ma_at(df_1h, 25)
    ma99_1h = _ma_at(df_1h, 99)
    res_1h, sup_1h = _nearest_levels(df_1h, current_price, n_swing=5)
    structure_1h = {
        "nearest_resistance": res_1h,
        "nearest_support": sup_1h,
        "price_vs_ma25_pct": (
            _safe_pct(current_price - ma25_1h, ma25_1h) if ma25_1h is not None else None
        ),
        "price_vs_ma99_pct": (
            _safe_pct(current_price - ma99_1h, ma99_1h) if ma99_1h is not None else None
        ),
        "structural_intact_for_direction": _structural_intact(df_1h, direction),
        "atr_pct": _safe_pct(atr(df_1h, period=14), current_price),
        "last_3_candles": [_compact_candle(df_1h.iloc[i]) for i in range(-3, 0)] if len(df_1h) >= 3 else [],
    }

    # 15m microstructure
    last_candle_15m = df_15m.iloc[-1].to_dict() if len(df_15m) else None
    last_pattern = _candle_pattern(last_candle_15m) if last_candle_15m else "unknown"
    microstructure_15m = {
        "last_candle_pattern": last_pattern,
        "consecutive_same_color": _consecutive_same_color(df_15m),
        "volume_last_4_vs_prior_12_ratio": _volume_ratio(df_15m, recent_n=4, baseline_n=12),
        "atr_pct": _safe_pct(atr(df_15m, period=14), current_price),
        "last_3_candles": [_compact_candle(df_15m.iloc[i]) for i in range(-3, 0)] if len(df_15m) >= 3 else [],
    }

    trade = {
        "symbol": symbol,
        "direction": direction,
        "entry": round(entry, 4),
        "current_price": round(current_price, 4),
        "pnl_pct": round(pnl_pct, 3),
    }

    return {
        "trade": trade,
        "trend_4h": trend_4h,
        "structure_1h": structure_1h,
        "microstructure_15m": microstructure_15m,
    }
