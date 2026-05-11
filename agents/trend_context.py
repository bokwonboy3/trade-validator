"""Trend Context Specialist — multi-timeframe trend alignment + slope.

Tier 1 Layer 1만 보면 4H MA25 vs MA99 정렬 여부 (binary). 이 specialist는:
- 1H / 4H / Daily MA가 모두 같은 방향인가? (multi-timeframe 합치)
- MA slope: 추세가 가속 중 vs 감속 중 vs 평탄?
- 최근 crossover 시점 (오래된 vs 막 일어난)
- 가격이 MA들로부터 얼마나 떨어져 있는가 (확장 vs 수렴)

거시적 trend health view 제공.
"""
from __future__ import annotations

import json
from typing import Final

import pandas as pd

from agents.client import AgentClient
from agents.specialist_base import run_specialist
from agents.types import SpecialistOutput
from analysis.indicators import add_ma, latest_ma
from analysis.layers import Direction

NAME: Final = "trend_context"

SYSTEM_PROMPT: Final = """\
You are a multi-timeframe trend specialist for a BTC perpetual-futures swing
framework. Tier 1's Layer 1 only checks 4H MA25 vs MA99 (binary). Your job is
to evaluate the BROADER trend context across timeframes:

- Are 1H, 4H, Daily MAs all aligned with the trade direction?
- Is the trend ACCELERATING (slope getting steeper), DECELERATING (slope
  flattening), or SIDEWAYS?
- Recent MA gap behavior — widening (strong trend) or narrowing (regime change)?
- Price extension from MAs — overextended (mean-reversion risk) vs healthy?

Output STRICT JSON only — no prose, no markdown fences:

{
  "trend_strength": 1-10,
  "trend_quality": "clean" | "choppy" | "consolidating" | "regime_change",
  "concerns": ["array of short Korean concern strings"],
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean explaining the assessment"
}

Be honest. If data is ambiguous, low confidence. Don't invent trends.
"""

EXPECTED_KEYS: Final = ["trend_strength", "trend_quality", "concerns"]


def _ma_summary(df: pd.DataFrame, label: str) -> dict:
    """Compute MA25/99 + slopes for a timeframe."""
    df = add_ma(df, [25, 99])
    if len(df) < 99:
        return {"label": label, "insufficient_data": True}
    ma25 = latest_ma(df, 25)
    ma99 = latest_ma(df, 99)
    # Slope: change in MA over last 5 bars (small lookback for recent momentum)
    ma25_series = df["ma_25"]
    slope_pct = float((ma25_series.iloc[-1] - ma25_series.iloc[-6]) / ma25_series.iloc[-6])
    return {
        "label": label,
        "ma25": round(ma25, 4),
        "ma99": round(ma99, 4),
        "gap_pct": round((ma25 - ma99) / max(abs(ma25), abs(ma99)), 5),
        "ma25_slope_pct_5bars": round(slope_pct, 5),
        "last_close": round(float(df["close"].iloc[-1]), 4),
    }


def build_user_content(
    *,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    df_daily: pd.DataFrame | None,
    direction: Direction,
) -> str:
    payload = {
        "direction": direction,
        "trend_view": {
            "1H": _ma_summary(df_1h, "1H"),
            "4H": _ma_summary(df_4h, "4H"),
            "Daily": _ma_summary(df_daily, "Daily") if df_daily is not None else {"insufficient_data": True},
        },
    }
    return (
        "다음 multi-timeframe trend 데이터에 대해 평가를 JSON으로 출력하세요.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def evaluate(
    client: AgentClient,
    *,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    df_daily: pd.DataFrame | None = None,
    direction: Direction,
) -> SpecialistOutput:
    user_content = build_user_content(
        df_1h=df_1h, df_4h=df_4h, df_daily=df_daily, direction=direction,
    )
    return run_specialist(
        client=client,
        name=NAME,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        expected_keys=EXPECTED_KEYS,
    )
