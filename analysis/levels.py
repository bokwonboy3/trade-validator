"""Swing high/low detection + support/resistance summarization.

A swing high at index i is a high strictly greater than the high of every
candle in [i-n, i-1] and [i+1, i+n]. Symmetric for swing low.

`compute_sr` groups swing levels (and optionally MAs) by their position
relative to a reference price — anything above is resistance, anything below
is support. The nearest few of each side are what a trader actually watches
when deciding "wait for X price" or "exit if Y breaks".
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" or "low"


@dataclass(frozen=True)
class SupportResistance:
    """Levels above/below a reference price.

    Each entry is (label, price). `resistance` is sorted ascending (nearest
    first). `support` is sorted descending (nearest first). Both capped to
    `max_levels` per side by `compute_sr`.
    """

    resistance: list[tuple[str, float]] = field(default_factory=list)
    support: list[tuple[str, float]] = field(default_factory=list)

    def nearest_resistance(self) -> tuple[str, float] | None:
        return self.resistance[0] if self.resistance else None

    def nearest_support(self) -> tuple[str, float] | None:
        return self.support[0] if self.support else None


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


def compute_sr(
    df: pd.DataFrame,
    reference_price: float,
    *,
    n_swing: int = 5,
    include_ma: bool = True,
    max_levels: int = 3,
) -> SupportResistance:
    """Return the nearest support / resistance levels relative to ``reference_price``.

    Source levels:
      - swing highs / lows detected by ``find_swings(df, n=n_swing)``
      - latest MA(25) and MA(99) of ``df['close']`` (if ``include_ma`` and
        the frame has enough rows to compute them)

    A level above the reference is considered **resistance**; below is **support**.
    Levels exactly equal to the reference are dropped (no actionable distance).
    Each side is sorted by nearness to the reference and capped to ``max_levels``.
    """
    if max_levels < 1:
        raise ValueError("max_levels must be >= 1")

    levels: list[tuple[str, float]] = []
    for s in find_swings(df, n=n_swing):
        levels.append((f"swing_{s.kind}", s.price))

    if include_ma and "close" in df.columns:
        if len(df) >= 25:
            ma25 = float(df["close"].rolling(25).mean().iloc[-1])
            if not pd.isna(ma25):
                levels.append(("ma25", ma25))
        if len(df) >= 99:
            ma99 = float(df["close"].rolling(99).mean().iloc[-1])
            if not pd.isna(ma99):
                levels.append(("ma99", ma99))

    above = sorted(
        ((lbl, p) for lbl, p in levels if p > reference_price),
        key=lambda lp: lp[1],
    )[:max_levels]
    below = sorted(
        ((lbl, p) for lbl, p in levels if p < reference_price),
        key=lambda lp: -lp[1],
    )[:max_levels]
    return SupportResistance(resistance=above, support=below)
