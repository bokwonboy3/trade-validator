"""Position Advisor — periodic LLM evaluation of an *open* trade.

Distinct from agents.runner (which evaluates *new entries*). The question here
is "given this open position + current market state, what action?", not
"should we enter?".

Single LLM call (not 5 specialists). Trade-off:
- Faster: ~10 sec vs ~15 sec parallel-specialists
- Simpler: one prompt, one JSON output
- Position-eval question is a single unified judgment — diverse viewpoints
  add less value than for entry-eval

Output: HOLD / TIGHTEN / PARTIAL / EXIT recommendation + rationale.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Literal

import pandas as pd

from agents.backend import get_default_client
from agents.client import AgentClient, AgentClientError
from analysis.layers import Direction

Action = Literal["HOLD", "TIGHTEN", "PARTIAL", "EXIT"]

VALID_ACTIONS: Final[set[str]] = {"HOLD", "TIGHTEN", "PARTIAL", "EXIT"}


@dataclass(frozen=True)
class AdvisorOutput:
    """Result from one advisor evaluation."""

    action: Action
    confidence: int  # 1~10
    rationale: str
    failed: bool = False
    failure_reason: str = ""


SYSTEM_PROMPT: Final = """\
You are a position-management advisor for a BTC perpetual-futures swing-trading
framework. The user is ALREADY in an open trade and wants to know what to do
*now* — they are NOT looking for new entries.

Given the trade context (entry, direction, current PnL%) and the current
market snapshot (4H trend, 1H structure, 15m microstructure, recent volume),
recommend ONE of four actions:

- HOLD: thesis intact, no action needed. Continue holding.
- TIGHTEN: thesis still intact but momentum cooling — move SL to break-even
  or tighten to lock in some profit.
- PARTIAL: take some profit (e.g. 50%) but keep a runner. Use when target
  region reached but trend still alive.
- EXIT: thesis broken — recommend closing the full position now.

Be HONEST and CONSERVATIVE:
- If data is ambiguous, default to HOLD (don't over-react to noise).
- Only recommend EXIT if there's a clear thesis-breaking signal (trend flip,
  structural breakdown, decisive volume against the position).
- Confidence: 1~10, where 1 = wild guess, 10 = unambiguous clear signal.

Output STRICT JSON only — no prose before or after, no markdown fences:

{
  "action": "HOLD" | "TIGHTEN" | "PARTIAL" | "EXIT",
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean explaining the recommendation"
}
"""


def _compact_candles(df: pd.DataFrame, n: int) -> list[dict]:
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
    symbol: str,
    direction: Direction,
    entry: float,
    current_price: float,
    pnl_pct: float,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> str:
    """Serialize trade + market context for the advisor prompt."""
    payload = {
        "trade": {
            "symbol": symbol,
            "direction": direction,
            "entry": round(entry, 4),
            "current_price": round(current_price, 4),
            "unrealized_pnl_pct": round(pnl_pct, 3),
        },
        "candles_4h_last_10": _compact_candles(df_4h, 10),
        "candles_1h_last_12": _compact_candles(df_1h, 12),
        "candles_15m_last_16": _compact_candles(df_15m, 16),
    }
    return (
        "다음 open position + 시장 데이터에 대해 position advisor 평가를 "
        "JSON으로 출력하세요.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def evaluate(
    *,
    symbol: str,
    direction: Direction,
    entry: float,
    current_price: float,
    pnl_pct: float,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    client: AgentClient | None = None,
) -> AdvisorOutput:
    """Run advisor on a single open trade. Returns AdvisorOutput.

    On API/network failure: returns AdvisorOutput(failed=True, ...). Caller
    decides whether to alert despite failure."""
    if client is None:
        client = get_default_client()
    if client is None:
        return AdvisorOutput(
            action="HOLD",
            confidence=0,
            rationale="(no agent backend available)",
            failed=True,
            failure_reason="no backend",
        )

    user_content = build_user_content(
        symbol=symbol, direction=direction,
        entry=entry, current_price=current_price, pnl_pct=pnl_pct,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    try:
        body = client.call_structured(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
        )
    except AgentClientError as e:
        return AdvisorOutput(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=str(e),
        )

    action = body.get("action")
    if action not in VALID_ACTIONS:
        return AdvisorOutput(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=f"invalid action: {action!r}",
        )
    try:
        confidence = int(body["confidence"])
    except (TypeError, ValueError, KeyError):
        return AdvisorOutput(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=f"confidence not int: {body.get('confidence')!r}",
        )
    confidence = max(1, min(10, confidence))
    rationale = str(body.get("rationale", ""))[:500]
    return AdvisorOutput(action=action, confidence=confidence, rationale=rationale)
