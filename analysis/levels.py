"""Swing high/low detection.

A swing high at index i is a high strictly greater than the high of every
candle in [i-n, i-1] and [i+1, i+n]. Symmetric for swing low.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" or "low"


def find_swings(df: pd.DataFrame, n: int = 5) -> list[Swing]:
    """Return all swing highs and swing lows in order of index.

    Edge candles (first n and last n) are skipped — they cannot be confirmed
    swings without future/past context.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    out: list[Swing] = []
    for i in range(n, len(df) - n):
        h = highs[i]
        if all(h > highs[j] for j in range(i - n, i)) and all(
            h > highs[j] for j in range(i + 1, i + n + 1)
        ):
            out.append(Swing(index=i, price=float(h), kind="high"))
            continue
        lo = lows[i]
        if all(lo < lows[j] for j in range(i - n, i)) and all(
            lo < lows[j] for j in range(i + 1, i + n + 1)
        ):
            out.append(Swing(index=i, price=float(lo), kind="low"))
    return out


def swing_highs(df: pd.DataFrame, n: int = 5) -> list[Swing]:
    return [s for s in find_swings(df, n) if s.kind == "high"]


def swing_lows(df: pd.DataFrame, n: int = 5) -> list[Swing]:
    return [s for s in find_swings(df, n) if s.kind == "low"]
