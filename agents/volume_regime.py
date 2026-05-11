"""Volume Regime Specialist — accumulation vs distribution.

Tier 1 Layer 3는 거부 캔들 거래량 ≥ 1.5x avg만 봄 (단일 시점). 이 specialist는:
- 최근 50개 15m 거래량 흐름이 increasing/decreasing 추세인가?
- 가격 상승 시 거래량 증가? (accumulation) 또는 거래량 감소? (distribution)
- 평균 대비 unusual spike 있나? (whale activity 신호)
"""
from __future__ import annotations

import json
from typing import Final

import pandas as pd

from agents.client import AgentClient
from agents.specialist_base import run_specialist
from agents.types import SpecialistOutput
from analysis.layers import Direction

NAME: Final = "volume_regime"

SYSTEM_PROMPT: Final = """\
You are a volume regime specialist for a BTC perpetual-futures swing framework.
Tier 1 Layer 3 only checks a single rejection candle's volume vs 1.5x baseline
(binary). Your job is to assess the BROADER volume regime:

- ACCUMULATION: price up + rising volume, or sideways + rising volume
- DISTRIBUTION: price up + declining volume (top), or down + rising volume (panic)
- NEUTRAL: no clear pattern, balanced

Also flag unusual activity (spike > 3x of recent avg, possible whale move).

Output STRICT JSON only — no prose, no markdown fences:

{
  "regime": "accumulation" | "distribution" | "neutral",
  "unusual_activity": true | false,
  "comparison_to_avg": "ratio of latest few candles' volume to recent avg, 1.0 = avg",
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean"
}

Be honest. Volume data alone is noisy — say "neutral" with mid confidence
when pattern unclear.
"""

EXPECTED_KEYS: Final = ["regime", "unusual_activity", "comparison_to_avg"]


def _compact_ohlcv(df: pd.DataFrame, n: int) -> list[dict]:
    rows = df.tail(n)
    return [
        {
            "c": round(float(r["close"]), 4),
            "v": round(float(r["volume"]), 2),
        }
        for _, r in rows.iterrows()
    ]


def build_user_content(
    *,
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame,
    direction: Direction,
) -> str:
    payload = {
        "direction": direction,
        "candles_15m_last_30": _compact_ohlcv(df_15m, 30),
        "candles_1h_last_15": _compact_ohlcv(df_1h, 15),
        "recent_15m_avg_volume": round(
            float(df_15m["volume"].tail(20).mean()), 2
        ),
    }
    return (
        "다음 거래량 프로파일을 분석하여 regime을 판단하세요. JSON only.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def evaluate(
    client: AgentClient,
    *,
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame,
    direction: Direction,
) -> SpecialistOutput:
    user_content = build_user_content(
        df_15m=df_15m, df_1h=df_1h, direction=direction,
    )
    return run_specialist(
        client=client,
        name=NAME,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        expected_keys=EXPECTED_KEYS,
    )
