"""Microstructure Specialist — 캔들 형태 + level 상호작용 깊이 분석.

Tier 1 Layer 3가 binary (hammer + 1.5x volume = yes/no)인 반면, 이 specialist는:
- 거부의 *질* (clean rejection / weak wick / fake / not present)
- forming 진행도 (얼마나 형성됐는지)
- false positive 위험 (wick은 있는데 거래량 빈약 등)
- level과의 상호작용 (단순 터치인지 진짜 반응인지)

를 연속적/언어적으로 평가. Tier 1이 못 보는 nuance를 보강.
"""
from __future__ import annotations

import json
from typing import Final

import pandas as pd

from agents.client import AgentClient
from agents.specialist_base import run_specialist
from agents.types import SpecialistOutput
from analysis.layers import Direction

NAME: Final = "microstructure"

SYSTEM_PROMPT: Final = """\
You are a microstructure specialist for a BTC perpetual-futures swing-trading
framework. The framework's Layer 3 (deterministic rule) checks for hammer/
shooting-star candles with 1.5x volume — but it gives a binary yes/no answer.

Your job: assess the *quality* of the rejection signal, beyond the binary rule.
Look at the 1m + 5m + 15m candle sequence near the entry level and answer:

- Is this a CLEAN rejection (clear price reversal + supporting volume)?
- WEAK rejection (wick exists but volume thin, or wick small)?
- FALSE POSITIVE risk (e.g., wick on low-liquidity drift)?
- NONE (no rejection visible at all)?

Also assess forming progress: if the 15m candle is still in progress, how far
along is the rejection pattern? Could it disappear by close?

Output STRICT JSON only — no prose before or after, no markdown fences:

{
  "rejection_quality": "clean" | "weak" | "false_positive_risk" | "none",
  "forming_progress": "n/a" | "early" | "mid" | "late",
  "false_positive_risk": "low" | "medium" | "high",
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean explaining the assessment"
}

Be honest and conservative — if the data is ambiguous, say so. Do not invent
patterns that aren't in the data.
"""

EXPECTED_KEYS: Final = ["rejection_quality", "forming_progress", "false_positive_risk"]


def _compact_candles(df: pd.DataFrame, n: int) -> list[dict]:
    """Take the last n candles, keep only OHLCV + openTime (compact for prompt)."""
    rows = df.tail(n)
    return [
        {
            "t": int(r["openTime"]),
            "o": round(float(r["open"]), 4),
            "h": round(float(r["high"]), 4),
            "l": round(float(r["low"]), 4),
            "c": round(float(r["close"]), 4),
            "v": round(float(r["volume"]), 2),
        }
        for _, r in rows.iterrows()
    ]


def build_user_content(
    *,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame,
    entry: float,
    direction: Direction,
    layer_3_status: str,
    layer_3_reason: str | None,
) -> str:
    """Serialize the relevant market data for the specialist's prompt."""
    payload = {
        "direction": direction,
        "entry": round(entry, 4),
        "layer3_deterministic": {
            "status": layer_3_status,
            "reason": layer_3_reason,
        },
        "candles_15m_last_8": _compact_candles(df_15m, 8),
        "candles_1m_last_20": _compact_candles(df_1m, 20),
    }
    return (
        "다음 시장 데이터에 대해 microstructure 평가를 JSON으로 출력하세요.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def evaluate(
    client: AgentClient,
    *,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame,
    entry: float,
    direction: Direction,
    layer_3_status: str,
    layer_3_reason: str | None = None,
) -> SpecialistOutput:
    """Run the microstructure specialist for a given setup."""
    user_content = build_user_content(
        df_15m=df_15m,
        df_1m=df_1m,
        entry=entry,
        direction=direction,
        layer_3_status=layer_3_status,
        layer_3_reason=layer_3_reason,
    )
    return run_specialist(
        client=client,
        name=NAME,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        expected_keys=EXPECTED_KEYS,
    )
