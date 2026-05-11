"""Indicator calculations (MA only for Phase 0)."""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def add_ma(df: pd.DataFrame, periods: Iterable[int]) -> pd.DataFrame:
    """Add MA columns named ``ma_<period>`` based on close price.

    Returns the same frame with new columns added (mutates a copy).
    """
    out = df.copy()
    for p in periods:
        out[f"ma_{p}"] = out["close"].rolling(window=p).mean()
    return out


def latest_ma(df_with_ma: pd.DataFrame, period: int) -> float:
    """Return the most recent MA value for ``period``. NaN if insufficient data."""
    return float(df_with_ma[f"ma_{period}"].iloc[-1])


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range over ``period`` candles, as a float (NaN if insufficient).

    True Range = max(high-low, |high - prev_close|, |low - prev_close|).
    Used by Risk Specialist to assess SL/TP placement vs current volatility.
    """
    if len(df) < 2:
        return float("nan")
    h_l = df["high"] - df["low"]
    prev_close = df["close"].shift(1)
    h_pc = (df["high"] - prev_close).abs()
    l_pc = (df["low"] - prev_close).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return float(tr.tail(period).mean())
