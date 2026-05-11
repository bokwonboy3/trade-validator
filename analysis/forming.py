"""In-progress 15m candle rejection detector (FORMING tier).

Aggregates 1m candles within the current in-progress 15m window into a single
"virtual 15m candle", then applies the same hammer/shooting-star + volume-spike
criteria as confirmed Layer 3.

Trader rule preservation: a FORMING signal is *not* a CONFIRMED signal. It is
explicitly labelled differently in the alert and uses a separate idempotency
signature so it doesn't suppress a later CONFIRMED alert on the same setup.
The volume threshold is the *same strict 1.5x* as confirmed — i.e., the partial
15m window must already exceed the 15m baseline. Linear scaling is *not* used,
to avoid early-window false positives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import pandas as pd

from analysis.candles import (
    VOLUME_MULTIPLIER,
    is_rejection,
    is_volume_spike,
)
from analysis.layers import Direction

FIFTEEN_MIN_MS: Final[int] = 15 * 60 * 1000
FORMING_TOUCH_TOLERANCE_PCT: Final[float] = 0.003  # match Layer 3
FORMING_MIN_MINUTES: Final[int] = 2  # need >=2 minutes of 1m data to evaluate


@dataclass(frozen=True)
class FormingResult:
    """A forming rejection signal in the in-progress 15m window."""

    virtual_open: float
    virtual_high: float
    virtual_low: float
    virtual_close: float
    virtual_volume: float
    minutes_elapsed: int
    window_open_ms: int

    @property
    def minutes_remaining(self) -> int:
        return max(0, 15 - self.minutes_elapsed)


def _current_15m_open_ms(now_ms: int) -> int:
    """Floor `now_ms` to the most recent 15m boundary."""
    return (now_ms // FIFTEEN_MIN_MS) * FIFTEEN_MIN_MS


def detect_forming_rejection(
    df_1m: pd.DataFrame,
    df_15m_history: pd.DataFrame,
    entry: float,
    direction: Direction,
    *,
    now_ms: int,
    touch_tolerance_pct: float = FORMING_TOUCH_TOLERANCE_PCT,
    vol_multiplier: float = VOLUME_MULTIPLIER,
    min_minutes: int = FORMING_MIN_MINUTES,
) -> FormingResult | None:
    """Detect a rejection pattern forming inside the current in-progress 15m window.

    Returns None when:
      - the in-progress 15m window has too few 1m candles yet (< min_minutes)
      - the virtual candle's [low, high] does not overlap entry ±tolerance
      - the aggregated wick/body ratio doesn't match a rejection pattern
      - cumulative volume hasn't yet exceeded the 15m baseline × multiplier

    The volume comparison is strict against the full 15m baseline — partial
    window must already exceed 1.5×. This biases toward strong, early spikes
    (the exact case we want to catch) and against weak signals that drift
    toward the threshold by the end.
    """
    if len(df_1m) == 0 or len(df_15m_history) == 0:
        return None

    window_open = _current_15m_open_ms(now_ms)
    window_close = window_open + FIFTEEN_MIN_MS
    in_window = df_1m[(df_1m["openTime"] >= window_open) & (df_1m["openTime"] < window_close)]
    minutes_elapsed = len(in_window)
    if minutes_elapsed < min_minutes:
        return None

    virtual_open = float(in_window.iloc[0]["open"])
    virtual_high = float(in_window["high"].max())
    virtual_low = float(in_window["low"].min())
    virtual_close = float(in_window.iloc[-1]["close"])
    virtual_volume = float(in_window["volume"].sum())

    # Zone overlap check (same logic as Layer 3 touch detection)
    zone_lo = entry * (1 - touch_tolerance_pct)
    zone_hi = entry * (1 + touch_tolerance_pct)
    if not (virtual_low <= zone_hi and virtual_high >= zone_lo):
        return None

    virtual_candle = {
        "open": virtual_open,
        "high": virtual_high,
        "low": virtual_low,
        "close": virtual_close,
        "volume": virtual_volume,
    }
    if not is_rejection(virtual_candle, direction):
        return None

    # Volume baseline = average of last 10 *closed* 15m candles.
    prev_vols = df_15m_history["volume"].tail(10).tolist()
    if not is_volume_spike(virtual_volume, prev_vols, multiplier=vol_multiplier):
        return None

    return FormingResult(
        virtual_open=virtual_open,
        virtual_high=virtual_high,
        virtual_low=virtual_low,
        virtual_close=virtual_close,
        virtual_volume=virtual_volume,
        minutes_elapsed=minutes_elapsed,
        window_open_ms=window_open,
    )
