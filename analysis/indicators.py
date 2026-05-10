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
