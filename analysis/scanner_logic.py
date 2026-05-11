"""Setup synthesis from market data — used by the multi-symbol scanner.

Given a symbol's recent OHLCV, derive a candidate (direction, entry, SL, TP)
that the 5-Layer framework can evaluate. The framework rules are upheld by
construction:
- direction is taken from Layer 1 (4H trend), so Layer 1 always passes
- SL is placed within Layer 4 tolerance below the closest qualifying swing
- TP is set so R:R == default_rr, so Layer 5 always passes

The discriminating layers in scanner mode are Layer 2 (entry near key level)
and Layer 3 (rejection trigger near entry). Layer 4 may still fail if the SL
buffer math goes outside tolerance, which guards against bad data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import pandas as pd

from analysis.indicators import latest_ma
from analysis.layers import Direction, LAYER4_SL_TOLERANCE_PCT
from analysis.levels import find_swings

DEFAULT_SL_BUFFER_PCT: Final[float] = 0.003   # 0.3% beyond swing
DEFAULT_SL_MAX_DISTANCE_PCT: Final[float] = 0.02  # don't go more than 2% for SL


@dataclass(frozen=True)
class SynthesizedSetup:
    direction: Direction
    entry: float
    sl: float
    tp: float
    sl_swing_price: float  # for diagnostics / output


def determine_direction(df_4h_with_ma: pd.DataFrame) -> Direction | None:
    """Layer-1 mirror: long when MA25 > MA99, short when MA25 < MA99, None on ties/NaN.

    Tied or NaN MAs mean no clear trend — skip the symbol.
    """
    ma25 = latest_ma(df_4h_with_ma, 25)
    ma99 = latest_ma(df_4h_with_ma, 99)
    if pd.isna(ma25) or pd.isna(ma99):
        return None
    if ma25 > ma99:
        return "long"
    if ma25 < ma99:
        return "short"
    return None


def synthesize_setup(
    df_1h: pd.DataFrame,
    direction: Direction,
    *,
    n_swing: int = 5,
    sl_buffer_pct: float = DEFAULT_SL_BUFFER_PCT,
    sl_max_distance_pct: float = DEFAULT_SL_MAX_DISTANCE_PCT,
    default_rr: float = 3.0,
) -> SynthesizedSetup | None:
    """Build a candidate setup from the 1H frame.

    Entry = last 1H close. SL = closest qualifying swing on the protective side,
    placed a buffer beyond. TP = entry ± default_rr × risk.

    Returns None when no swing of the right kind exists within
    sl_max_distance_pct of entry — the symbol has no clean structural SL.
    """
    if sl_buffer_pct >= LAYER4_SL_TOLERANCE_PCT:
        # Buffer must stay strictly inside Layer 4 tolerance, otherwise Layer 4 fails
        # for synthesized setups by construction. Surface as a programming error.
        raise ValueError(
            f"sl_buffer_pct ({sl_buffer_pct}) must be < LAYER4_SL_TOLERANCE_PCT "
            f"({LAYER4_SL_TOLERANCE_PCT})"
        )

    if len(df_1h) == 0:
        return None
    entry = float(df_1h.iloc[-1]["close"])
    swings = find_swings(df_1h, n=n_swing)

    if direction == "long":
        candidates = [
            s.price
            for s in swings
            if s.kind == "low"
            and s.price < entry
            and (entry - s.price) / entry <= sl_max_distance_pct
        ]
        if not candidates:
            return None
        sl_swing = max(candidates)  # closest below entry
        sl = sl_swing * (1 - sl_buffer_pct)
        risk = entry - sl
        tp = entry + default_rr * risk
    else:
        candidates = [
            s.price
            for s in swings
            if s.kind == "high"
            and s.price > entry
            and (s.price - entry) / entry <= sl_max_distance_pct
        ]
        if not candidates:
            return None
        sl_swing = min(candidates)  # closest above entry
        sl = sl_swing * (1 + sl_buffer_pct)
        risk = sl - entry
        tp = entry - default_rr * risk

    return SynthesizedSetup(
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        sl_swing_price=sl_swing,
    )
