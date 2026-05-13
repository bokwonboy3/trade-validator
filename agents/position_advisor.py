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
from agents.sdk_client import ImagePart, VisionClient, VisionClientError
from agents.vision_config import vision_advisor_enabled, vision_override_is_dry_run
from analysis.chart_renderer import TradeLevels, render_composite_chart
from analysis.features import build_semantic_context
from analysis.layers import Direction

Action = Literal["HOLD", "TIGHTEN", "PARTIAL", "EXIT"]

VALID_ACTIONS: Final[set[str]] = {"HOLD", "TIGHTEN", "PARTIAL", "EXIT"}

# Conservatism rank — higher = more defensive. Used by ``_merge_actions`` to
# enforce "vision can only push toward MORE conservative" (mirrors the
# downgrade-only principle in agents.types).
ACTION_RANK: Final[dict[str, int]] = {
    "HOLD": 0,
    "TIGHTEN": 1,
    "PARTIAL": 2,
    "EXIT": 3,
}


@dataclass(frozen=True)
class VisionVerdict:
    """Result of the vision second-opinion call. Always advisory — the merge
    rule in ``evaluate`` decides whether to enforce or just log it."""

    action: Action
    confidence: int  # 1~10
    rationale: str
    failed: bool = False
    failure_reason: str = ""


@dataclass(frozen=True)
class AdvisorOutput:
    """Result from one advisor evaluation.

    Fields ``vision`` / ``vision_overrode`` / ``dry_run`` are populated only
    when the vision tier ran. Pre-vision callers that read action /
    confidence / rationale see the final (possibly overridden) values."""

    action: Action
    confidence: int  # 1~10
    rationale: str
    failed: bool = False
    failure_reason: str = ""
    # PR-2 (Phase 8c): vision second-opinion telemetry.
    vision: VisionVerdict | None = None
    vision_overrode: bool = False
    dry_run: bool = False


SYSTEM_PROMPT: Final = """\
You are a position-management advisor for a BTC perpetual-futures swing-trading
framework. The user is ALREADY in an open trade and wants to know what to do
*now* — they are NOT looking for new entries.

You will receive a STRUCTURED MARKET CONTEXT (not raw candles). Python has
already computed: trend alignment, MA distances, nearest support/resistance,
slope, volume ratios, last candle pattern, structural integrity for the
trade direction, ATR%, and a small 3-candle verification snapshot per
timeframe. Treat these as ground truth; reason ON them, not from them.

Recommend ONE of four actions:

- HOLD: thesis intact, no action needed. Continue holding.
- TIGHTEN: thesis still intact but momentum cooling — move SL to break-even
  or tighten to lock in some profit.
- PARTIAL: take some profit (e.g. 50%) but keep a runner. Use when target
  region reached but trend still alive.
- EXIT: thesis broken — recommend closing the full position now.

Reasoning anchors:
- "structural_intact_for_direction = false" is a meaningful break — strong
  signal toward TIGHTEN or EXIT depending on PnL.
- "alignment" flipped against the trade direction = thesis flipped.
- "nearest_resistance.distance_pct" small + LONG profit ⇒ consider PARTIAL/TIGHTEN.
- A "null" field means insufficient data — say so in the rationale, don't
  guess.

Be HONEST and CONSERVATIVE:
- If signals conflict or are weak, default to HOLD.
- Only EXIT on a clear thesis-breaking signal (alignment flip, structural
  break with momentum, decisive opposing volume).
- Confidence: 1~10. Anchor: 1 = guess, 10 = unambiguous, 5 = balanced.

Output STRICT JSON only — no prose before or after, no markdown fences:

{
  "action": "HOLD" | "TIGHTEN" | "PARTIAL" | "EXIT",
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean explaining the recommendation"
}
"""


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
    """Serialize trade + semantic market context for the advisor prompt.

    Phase 8b: this used to dump ~38 raw candles (~3K tokens). Now it ships
    pre-computed features (trend alignment, MA distances, nearest S/R,
    structural integrity, volume ratios, last-candle pattern) via
    ``build_semantic_context`` — ~400 tokens of *answers* the LLM would
    have spent its budget recomputing.
    """
    payload = build_semantic_context(
        symbol=symbol, direction=direction,
        entry=entry, current_price=current_price, pnl_pct=pnl_pct,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    return (
        "다음 open position의 사전 분석된 시장 컨텍스트를 보고 advisor 평가를 "
        "JSON으로만 출력하세요.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


VISION_SYSTEM_PROMPT: Final = """\
You are the VISION SECOND-OPINION layer of a position advisor. The user is
already in an open trade. A text-only advisor has already produced an action
recommendation from PRE-COMPUTED FEATURES (alignment, MAs, slopes, S/R,
structural integrity, ATR%). Your job is independent: look at the attached
4H + 1H + 15m chart and decide whether the visual evidence agrees.

The chart shows three stacked timeframes, each with candles, MA25 (orange),
MA99 (blue), swing high (▼) / swing low (▲) markers, a volume sub-panel,
and an RSI(14) sub-panel. The 15m panel may also show dashed Entry / SL /
TP horizontal lines.

Things the chart shows that structured features can MISS:
- Multi-candle visual structural breaks (e.g., a clean break of a swing
  low after consolidation).
- Bearish / bullish divergence between price and RSI across several swings.
- Wedge / triangle / range exhaustion patterns spanning more candles than
  the feature window.
- Decisive rejection wicks at visible S/R that already have history.

Pick ONE action (same set as the text advisor): HOLD / TIGHTEN / PARTIAL / EXIT.

BIAS toward agreement with the text advisor unless the chart shows a clear
visual contradiction. You can only override TOWARD a more conservative
action (HOLD → TIGHTEN → PARTIAL → EXIT). NEVER suggest a less-conservative
action than the text advisor — the framework enforces downgrade-only at the
merge step regardless, but stay aligned to keep your rationale meaningful.

Output STRICT JSON only — no prose before or after, no markdown fences:

{
  "action": "HOLD" | "TIGHTEN" | "PARTIAL" | "EXIT",
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean naming the visual evidence"
}
"""


def _build_vision_user_content(
    *, user_content: str, text_action: Action, text_rationale: str,
) -> str:
    """User-text block accompanying the chart in the multimodal call.

    Includes the text advisor's tentative verdict so the vision call has
    full context (chart + features + prior decision) when deciding whether
    to confirm or escalate."""
    return (
        "다음은 텍스트 advisor가 이미 본 사전-분석 컨텍스트입니다. "
        "그리고 첨부 차트(4H + 1H + 15m)도 함께 검토해 시각적으로 동의/반대를 "
        "판단해 JSON으로만 답하세요.\n\n"
        f"[Text advisor tentative verdict]\n"
        f"action: {text_action}\n"
        f"rationale: {text_rationale}\n\n"
        "[Structured market context]\n"
        f"{user_content}"
    )


def _merge_actions(text_action: Action, vision_action: Action) -> tuple[Action, bool]:
    """Apply downgrade-only merge: vision can only push toward MORE conservative.

    Returns (final_action, overridden). ``overridden`` is True when the
    vision verdict actually changed the action."""
    if ACTION_RANK[vision_action] > ACTION_RANK[text_action]:
        return vision_action, True
    return text_action, False


def _run_vision_second_opinion(
    *,
    vision_client: VisionClient,
    user_content: str,
    text_action: Action,
    text_rationale: str,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    levels: TradeLevels | None,
) -> VisionVerdict:
    """Render the chart, send it with the structured context, parse a verdict.

    Catches every error path so a vision failure NEVER breaks the text-only
    advisor — the caller continues with the text decision."""
    try:
        chart_b64 = render_composite_chart(df_4h, df_1h, df_15m, levels=levels)
    except Exception as e:  # noqa: BLE001 — rendering is best-effort telemetry
        return VisionVerdict(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=f"chart render error: {e}",
        )

    vision_user = _build_vision_user_content(
        user_content=user_content,
        text_action=text_action,
        text_rationale=text_rationale,
    )
    try:
        body = vision_client.call_multimodal(
            system_prompt=VISION_SYSTEM_PROMPT,
            text_content=vision_user,
            images=[ImagePart(data=chart_b64, media_type="image/png")],
        )
    except VisionClientError as e:
        return VisionVerdict(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=str(e),
        )

    action = body.get("action")
    if action not in VALID_ACTIONS:
        return VisionVerdict(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=f"invalid action: {action!r}",
        )
    try:
        conf = int(body["confidence"])
    except (TypeError, ValueError, KeyError):
        return VisionVerdict(
            action="HOLD", confidence=0, rationale="",
            failed=True, failure_reason=f"confidence not int: {body.get('confidence')!r}",
        )
    return VisionVerdict(
        action=action,
        confidence=max(1, min(10, conf)),
        rationale=str(body.get("rationale", ""))[:500],
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
    stop_loss: float | None = None,
    take_profit: float | None = None,
    vision_client: VisionClient | None = None,
) -> AdvisorOutput:
    """Run advisor on a single open trade. Returns AdvisorOutput.

    On API/network failure: returns AdvisorOutput(failed=True, ...). Caller
    decides whether to alert despite failure.

    When ``vision_client`` is provided AND the vision-advisor toggle is on,
    the chart is rendered and sent to the vision model as a second opinion.
    Merge rule: vision can only push the action toward MORE conservative
    (HOLD < TIGHTEN < PARTIAL < EXIT). In dry-run mode the vision verdict
    is captured for telemetry but does not change the enforced action.
    """
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

    # Vision second-opinion (Phase 8c). Skipped if no client, toggle off,
    # or any rendering / API failure — text decision remains authoritative.
    if vision_client is None or not vision_advisor_enabled():
        return AdvisorOutput(
            action=action, confidence=confidence, rationale=rationale,
        )

    levels = TradeLevels(
        entry=entry, stop_loss=stop_loss, take_profit=take_profit,
    )
    vision = _run_vision_second_opinion(
        vision_client=vision_client,
        user_content=user_content,
        text_action=action,
        text_rationale=rationale,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
        levels=levels,
    )
    dry_run = vision_override_is_dry_run()
    if vision.failed or dry_run:
        # Vision is logged-only: record verdict, keep text action.
        return AdvisorOutput(
            action=action,
            confidence=confidence,
            rationale=rationale,
            vision=vision,
            vision_overrode=False,
            dry_run=dry_run,
        )

    final_action, overrode = _merge_actions(action, vision.action)
    if overrode:
        rationale = (
            f"{rationale}  [vision override → {vision.action}: {vision.rationale}]"
        )[:700]
    return AdvisorOutput(
        action=final_action,
        confidence=confidence,
        rationale=rationale,
        vision=vision,
        vision_overrode=overrode,
        dry_run=False,
    )
