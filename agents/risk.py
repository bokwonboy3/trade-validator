"""Risk Specialist — ATR-aware SL/TP appropriateness + expected hold time.

Tier 1 Layer 4는 SL이 swing 너머 ±0.5%인지만 봄. Layer 5는 R:R ≥ 3.0만 봄.
이 specialist는:
- SL 거리가 현재 ATR 대비 tight한가 optimal한가 loose한가?
  (ATR보다 너무 좁으면 noise로 hit, 너무 넓으면 risk 과다)
- TP까지 거리가 현재 변동성 기준 N시간 내 reachable한가?
- 예상 보유 시간 (TP까지 도달 시간 추정)
"""
from __future__ import annotations

import json
from typing import Final

import pandas as pd

from agents.client import AgentClient
from agents.specialist_base import run_specialist
from agents.types import SpecialistOutput
from analysis.indicators import atr
from analysis.layers import Direction

NAME: Final = "risk"

SYSTEM_PROMPT: Final = """\
You are a risk specialist for a BTC perpetual-futures swing framework. Tier 1
only checks structural SL placement (swing-based) and R:R ≥ 3.0. Your job:
assess SL/TP appropriateness given CURRENT volatility (ATR).

- SL 거리 vs ATR:
  - sl_distance < 0.5 * ATR → "tight" (noise stop-out risk high)
  - 0.5 * ATR ≤ sl_distance ≤ 2 * ATR → "optimal"
  - sl_distance > 2 * ATR → "loose" (risk over-sized)

- TP realism:
  - TP 거리 / ATR ratio. 만약 3-5 ATR move 필요하면 normal hold;
    10 ATR 이상이면 거의 도달 불가 (unlikely)

- 예상 hold 시간: TP 거리 / (현재 시간당 변동성) 대략 시간 단위 추정

Output STRICT JSON only:

{
  "sl_quality": "tight" | "optimal" | "loose",
  "tp_realism": "likely" | "uncertain" | "unlikely",
  "expected_hold_hours": integer 1-72,
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean"
}
"""

EXPECTED_KEYS: Final = ["sl_quality", "tp_realism", "expected_hold_hours"]


def build_user_content(
    *,
    entry: float,
    sl: float,
    tp: float,
    direction: Direction,
    atr_1h: float,
    atr_15m: float,
) -> str:
    sl_distance = abs(entry - sl)
    tp_distance = abs(tp - entry)
    payload = {
        "direction": direction,
        "entry": round(entry, 4),
        "sl": round(sl, 4),
        "tp": round(tp, 4),
        "sl_distance": round(sl_distance, 4),
        "tp_distance": round(tp_distance, 4),
        "atr_1h": round(atr_1h, 4),
        "atr_15m": round(atr_15m, 4),
        "sl_distance_in_atr_1h": round(sl_distance / atr_1h, 3) if atr_1h > 0 else None,
        "tp_distance_in_atr_1h": round(tp_distance / atr_1h, 3) if atr_1h > 0 else None,
        "rr_ratio": round(tp_distance / sl_distance, 3) if sl_distance > 0 else None,
    }
    return (
        "다음 SL/TP 셋업을 ATR 기반으로 평가하세요. JSON only.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def evaluate(
    client: AgentClient,
    *,
    entry: float,
    sl: float,
    tp: float,
    direction: Direction,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> SpecialistOutput:
    atr_1h = atr(df_1h, period=14)
    atr_15m = atr(df_15m, period=14)
    user_content = build_user_content(
        entry=entry, sl=sl, tp=tp, direction=direction,
        atr_1h=atr_1h, atr_15m=atr_15m,
    )
    return run_specialist(
        client=client,
        name=NAME,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        expected_keys=EXPECTED_KEYS,
    )
