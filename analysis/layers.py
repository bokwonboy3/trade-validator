"""5-Layer evaluation functions.

Each layer returns a ``LayerResult`` with:
- score: 0 or 1 (Layer 3 may also be 0 with status="pending")
- status: "pass" | "fail" | "pending"
- detail: dict with layer-specific values (closest_level, distance_pct, ma25, etc.)

Layer functions are kept pure where possible — they take dataframes and inputs,
and return results without performing I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal

import pandas as pd

from analysis.candles import (
    VOLUME_MULTIPLIER,
    is_rejection,
    is_volume_spike,
)
from analysis.indicators import add_ma, latest_ma
from analysis.levels import find_swings

Direction = Literal["long", "short"]
Status = Literal["pass", "fail", "pending"]

LAYER1_MIN_MA_GAP_PCT: Final[float] = 0.001  # 0.1% — minimum trend strength
LAYER2_TOLERANCE_PCT: Final[float] = 0.003  # ±0.3%
LAYER3_TOUCH_TOLERANCE_PCT: Final[float] = 0.003  # ±0.3%
LAYER4_SL_TOLERANCE_PCT: Final[float] = 0.005  # ±0.5%
LAYER5_MIN_RR: Final[float] = 3.0
ONE_HOUR_MS: Final[int] = 60 * 60 * 1000


class InputError(ValueError):
    """User input is inconsistent with the trade direction or zero R:R."""


@dataclass
class LayerResult:
    score: int
    status: Status
    detail: dict[str, Any] = field(default_factory=dict)


def layer_1_trend(
    df_4h_with_ma: pd.DataFrame,
    direction: Direction,
    *,
    min_ma_gap_pct: float = LAYER1_MIN_MA_GAP_PCT,
) -> LayerResult:
    """Layer 1: 4H MA(25) vs MA(99) alignment + minimum trend strength.

    LONG passes when MA25 > MA99, SHORT passes when MA25 < MA99.
    Equality or near-equality fails — when ``|ma25 - ma99| / max(ma25, ma99)``
    is below ``min_ma_gap_pct`` (default 0.1%), the trend is considered too weak
    to act on, regardless of which side is higher.

    근거 (Phase 2 tuning, 2026-05-11): adaptive-tuning loop iter 1~4 관찰에서
    ETHUSDT의 MA gap이 0.005~0.007% 사이에서 LONG↔SHORT 방향 flip하는 케이스를
    포착. trader 관점에서 그건 noise이지 추세 아님. min gap 0.1%는 그 noise
    band의 10배로, 명백한 추세 (BTC 2.26%, SOL 5.5% 등)에는 영향 없음.
    """
    ma25 = latest_ma(df_4h_with_ma, 25)
    ma99 = latest_ma(df_4h_with_ma, 99)
    detail: dict[str, Any] = {"ma25": ma25, "ma99": ma99}

    if pd.isna(ma25) or pd.isna(ma99):
        return LayerResult(score=0, status="fail", detail=detail)

    larger = max(abs(ma25), abs(ma99))
    if larger == 0:
        return LayerResult(score=0, status="fail", detail=detail)
    gap_pct = abs(ma25 - ma99) / larger
    detail["ma_gap_pct"] = gap_pct

    if gap_pct < min_ma_gap_pct:
        detail["reason"] = "weak_trend"
        detail["min_ma_gap_pct"] = min_ma_gap_pct
        return LayerResult(score=0, status="fail", detail=detail)

    if direction == "long":
        passed = ma25 > ma99
    else:
        passed = ma25 < ma99
    return LayerResult(
        score=1 if passed else 0,
        status="pass" if passed else "fail",
        detail=detail,
    )


def layer_2_setup_zone(
    df_1h_with_ma: pd.DataFrame,
    entry: float,
    *,
    n_swing: int = 5,
    tolerance_pct: float = LAYER2_TOLERANCE_PCT,
) -> LayerResult:
    """Layer 2: entry sits within ±tolerance of a key level.

    Key levels = swing highs + swing lows (n_swing) + latest MA25 + MA99.
    """
    levels: list[tuple[str, float]] = []
    for s in find_swings(df_1h_with_ma, n=n_swing):
        levels.append((f"swing_{s.kind}", s.price))
    ma25 = latest_ma(df_1h_with_ma, 25)
    ma99 = latest_ma(df_1h_with_ma, 99)
    if not pd.isna(ma25):
        levels.append(("ma25_1h", float(ma25)))
    if not pd.isna(ma99):
        levels.append(("ma99_1h", float(ma99)))

    if not levels:
        # No swings AND no MA values — insufficient data for evaluation.
        return LayerResult(
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

    # Closest level by absolute percent distance from entry
    best_label, best_price, best_dist = "", 0.0, float("inf")
    for label, price in levels:
        dist = abs(price - entry) / entry
        if dist < best_dist:
            best_label, best_price, best_dist = label, price, dist

    passed = best_dist <= tolerance_pct
    return LayerResult(
        score=1 if passed else 0,
        status="pass" if passed else "fail",
        detail={
            "closest_label": best_label,
            "closest_price": best_price,
            "distance_pct": best_dist,
            "all_levels": levels,
        },
    )


def _find_recent_touch_1h(
    df_1h: pd.DataFrame, entry: float, tolerance_pct: float
) -> int | None:
    """Index of the most recent 1H candle whose [low, high] overlaps entry zone.

    Returns None if no candle in df_1h touches the zone.
    """
    zone_lo = entry * (1 - tolerance_pct)
    zone_hi = entry * (1 + tolerance_pct)
    for i in range(len(df_1h) - 1, -1, -1):
        row = df_1h.iloc[i]
        if row["low"] <= zone_hi and row["high"] >= zone_lo:
            return i
    return None


def _slice_15m_for_1h(df_15m: pd.DataFrame, open_time_1h: int) -> pd.DataFrame:
    """Return up to 4 15m candles whose openTime falls within the 1h window."""
    start = open_time_1h
    end = open_time_1h + ONE_HOUR_MS
    mask = (df_15m["openTime"] >= start) & (df_15m["openTime"] < end)
    return df_15m.loc[mask].reset_index(drop=True)


def layer_3_rejection(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    entry: float,
    direction: Direction,
    *,
    touch_tolerance_pct: float = LAYER3_TOUCH_TOLERANCE_PCT,
    vol_multiplier: float = VOLUME_MULTIPLIER,
) -> LayerResult:
    """Layer 3 (Hybrid Historical): rejection candle near entry within last 50h.

    Algorithm:
      1. Find the most recent 1H candle whose [low, high] overlaps entry ±0.3%.
      2. If found: slice the 4 (or fewer) 15m candles inside that 1H window.
         Pass if ANY 15m candle is a direction-matching rejection
         AND has volume > 1.5× of its 10 preceding 15m candles' average.
      3. If no touch found: status = "pending", score = 0.
    """
    touch_idx = _find_recent_touch_1h(df_1h, entry, touch_tolerance_pct)
    if touch_idx is None:
        return LayerResult(
            score=0,
            status="pending",
            detail={"reason": "no_touch_in_history"},
        )

    touch_row = df_1h.iloc[touch_idx]
    open_time_1h = int(touch_row["openTime"])
    matching_15m = _slice_15m_for_1h(df_15m, open_time_1h)
    if len(matching_15m) == 0:
        # Touch found but 15m data doesn't cover that window.
        return LayerResult(
            score=0,
            status="pending",
            detail={
                "reason": "15m_data_missing_for_touch_window",
                "touch_open_time_ms": open_time_1h,
            },
        )

    rejection_idx = None
    for j in range(len(matching_15m)):
        c = matching_15m.iloc[j].to_dict()
        if not is_rejection(c, direction):
            continue
        global_idx_in_15m = df_15m.index[df_15m["openTime"] == c["openTime"]][0]
        prev_start = max(0, global_idx_in_15m - 10)
        prev_volumes = df_15m["volume"].iloc[prev_start:global_idx_in_15m].tolist()
        if is_volume_spike(c["volume"], prev_volumes, multiplier=vol_multiplier):
            rejection_idx = global_idx_in_15m
            break

    if rejection_idx is not None:
        rej_row = df_15m.iloc[rejection_idx]
        return LayerResult(
            score=1,
            status="pass",
            detail={
                "touch_open_time_ms": open_time_1h,
                "rejection_open_time_ms": int(rej_row["openTime"]),
                "rejection_volume": float(rej_row["volume"]),
            },
        )
    return LayerResult(
        score=0,
        status="fail",
        detail={
            "touch_open_time_ms": open_time_1h,
            "reason": "no_rejection_with_volume_in_touch_window",
        },
    )


def layer_4_sl_structure(
    df_1h: pd.DataFrame,
    sl: float,
    direction: Direction,
    *,
    n_swing: int = 5,
    tolerance_pct: float = LAYER4_SL_TOLERANCE_PCT,
) -> LayerResult:
    """Layer 4: SL is structure-based.

    LONG: SL must sit in [swing_low × (1 - tol), swing_low].
    SHORT: SL must sit in [swing_high, swing_high × (1 + tol)].

    The closest qualifying swing is selected.
    """
    swings = find_swings(df_1h, n=n_swing)
    if direction == "long":
        candidates = [s.price for s in swings if s.kind == "low"]
        # LONG SL must be below or equal to swing_low, within tolerance
        valid = [
            (price, sl - price)  # negative when SL below swing
            for price in candidates
            if price * (1 - tolerance_pct) <= sl <= price
        ]
    else:
        candidates = [s.price for s in swings if s.kind == "high"]
        valid = [
            (price, sl - price)
            for price in candidates
            if price <= sl <= price * (1 + tolerance_pct)
        ]

    if not valid:
        # Find closest swing of the right kind for diagnostic detail
        if candidates:
            closest = min(candidates, key=lambda p: abs(p - sl))
            dist_pct = abs(sl - closest) / closest
            detail = {
                "passed_swing_price": None,
                "closest_swing_price": closest,
                "distance_pct": dist_pct,
            }
        else:
            detail = {"passed_swing_price": None, "closest_swing_price": None}
        return LayerResult(score=0, status="fail", detail=detail)

    # Pick the swing where SL is closest to it (smallest |sl - price|)
    best_price, _ = min(valid, key=lambda t: abs(t[1]))
    return LayerResult(
        score=1,
        status="pass",
        detail={
            "passed_swing_price": best_price,
            "distance_pct": abs(sl - best_price) / best_price,
        },
    )


def validate_inputs(entry: float, sl: float, tp: float, direction: Direction) -> None:
    """Raise InputError if entry/SL/TP layout is inconsistent with direction."""
    if direction == "long":
        if sl >= entry:
            raise InputError(
                f"LONG: SL ({sl:,.2f}) must be below Entry ({entry:,.2f})"
            )
        if tp <= entry:
            raise InputError(
                f"LONG: TP ({tp:,.2f}) must be above Entry ({entry:,.2f})"
            )
    elif direction == "short":
        if sl <= entry:
            raise InputError(
                f"SHORT: SL ({sl:,.2f}) must be above Entry ({entry:,.2f})"
            )
        if tp >= entry:
            raise InputError(
                f"SHORT: TP ({tp:,.2f}) must be below Entry ({entry:,.2f})"
            )
    else:
        raise InputError(f"direction must be 'long' or 'short' (got: {direction!r})")


def layer_5_risk_reward(
    entry: float,
    sl: float,
    tp: float,
    direction: Direction,
    *,
    min_rr: float = LAYER5_MIN_RR,
) -> LayerResult:
    """Layer 5: R:R ≥ 3.0. Assumes inputs already validated."""
    if direction == "long":
        reward = tp - entry
        risk = entry - sl
    else:
        reward = entry - tp
        risk = sl - entry
    rr = reward / risk
    passed = rr >= min_rr
    return LayerResult(
        score=1 if passed else 0,
        status="pass" if passed else "fail",
        detail={"rr": rr, "reward": reward, "risk": risk, "min_rr": min_rr},
    )


PASS_THRESHOLD: Final[int] = 4


@dataclass(frozen=True)
class SetupEvaluation:
    """Bundled output of all 5 layers + score helpers."""

    layer_1: LayerResult
    layer_2: LayerResult
    layer_3: LayerResult
    layer_4: LayerResult
    layer_5: LayerResult

    @property
    def total_score(self) -> int:
        return (
            self.layer_1.score
            + self.layer_2.score
            + self.layer_3.score
            + self.layer_4.score
            + self.layer_5.score
        )

    @property
    def passes(self) -> bool:
        return self.total_score >= PASS_THRESHOLD

    def as_layers(self) -> list[LayerResult]:
        return [self.layer_1, self.layer_2, self.layer_3, self.layer_4, self.layer_5]


def evaluate_setup(
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    *,
    entry: float,
    sl: float,
    tp: float,
    direction: Direction,
) -> SetupEvaluation:
    """Run all 5 layers in order on the given OHLCV frames.

    Adds MA columns to the 4h and 1h frames as needed. Layer 3 uses raw 1h+15m
    (no MA needed). Inputs are assumed already validated via validate_inputs().
    """
    df_4h_ma = add_ma(df_4h, [25, 99])
    df_1h_ma = add_ma(df_1h, [25, 99])
    return SetupEvaluation(
        layer_1=layer_1_trend(df_4h_ma, direction),
        layer_2=layer_2_setup_zone(df_1h_ma, entry),
        layer_3=layer_3_rejection(df_1h, df_15m, entry, direction),
        layer_4=layer_4_sl_structure(df_1h, sl, direction),
        layer_5=layer_5_risk_reward(entry, sl, tp, direction),
    )
