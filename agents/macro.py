"""Macro Specialist — funding rate + open interest 흐름.

Tier 1은 가격/캔들/거래량만 봄. 이 specialist는 derivatives market 컨텍스트:
- Funding rate: 양/음, trend (과열인가 정상인가)
- Open Interest: 증가/감소 (포지션 빌딩 vs 청산)
- Combined: bullish/bearish 압력 추정

뉴스/소셜은 v1에서 제외 (외부 API 의존성 회피).
"""
from __future__ import annotations

import json
from typing import Final

from agents.client import AgentClient
from agents.specialist_base import run_specialist
from agents.types import SpecialistOutput
from analysis.layers import Direction

NAME: Final = "macro"

SYSTEM_PROMPT: Final = """\
You are a macro specialist for BTC perpetual futures. Tier 1 doesn't look at
derivatives data at all. Your job: assess macro pressure from funding rate +
open interest:

- FUNDING RATE (Binance perp 8h funding):
  - > +0.05% → very positive (longs paying shorts, possible overheating)
  - +0.01% ~ +0.05% → normal positive bias
  - -0.01% ~ +0.01% → neutral
  - < -0.01% → negative (shorts paying longs, possible squeeze setup)

- OPEN INTEREST trend (last 12h):
  - rising → position building (trend continuation likely)
  - falling → position closing (squeeze or distribution)
  - flat → no commitment shift

- COMBINED:
  - rising OI + positive funding → bullish but watch for blow-off
  - rising OI + negative funding → bearish accumulation
  - falling OI + extreme funding → mean-reversion likely

Output STRICT JSON only:

{
  "macro_bias": "bullish" | "bearish" | "neutral",
  "unusual_events": ["array of short Korean flags, e.g. '펀딩 과열', 'OI 급락'"],
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean"
}
"""

EXPECTED_KEYS: Final = ["macro_bias", "unusual_events"]


def build_user_content(
    *,
    funding_history: list[dict],
    oi_history: list[dict],
    direction: Direction,
) -> str:
    # Compact funding: [(time_iso, rate_pct), ...]
    funding_compact = [
        {
            "t_ms": int(f["fundingTime"]),
            "rate_pct": round(float(f["fundingRate"]) * 100, 4),
        }
        for f in funding_history
    ]
    oi_compact = [
        {
            "t_ms": int(o["timestamp"]),
            "oi": round(float(o["sumOpenInterest"]), 2),
        }
        for o in oi_history
    ]
    payload = {
        "direction": direction,
        "funding_last_8_periods_8h_each": funding_compact,
        "open_interest_last_12_periods_1h_each": oi_compact,
    }
    return (
        "다음 derivatives 데이터로 macro bias 평가. JSON only.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def evaluate(
    client: AgentClient,
    *,
    funding_history: list[dict],
    oi_history: list[dict],
    direction: Direction,
) -> SpecialistOutput:
    user_content = build_user_content(
        funding_history=funding_history,
        oi_history=oi_history,
        direction=direction,
    )
    return run_specialist(
        client=client,
        name=NAME,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        expected_keys=EXPECTED_KEYS,
    )
