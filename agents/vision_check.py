"""Pre-entry vision check (Phase 8c PR-3).

After the text-tier 5-specialist recommender produces an ``AgentVerdict``,
this module gives the multimodal model one final look at the *actual chart*
and lets it downgrade — but never upgrade — the verdict.

Why a separate check (vs. a 6th specialist)?
- Specialist runs are parallel and recommender fuses them rule-based. A
  vision pass produces a single ENTER/PLAN_OK/WATCH/PASS judgement; it's
  more naturally modeled as a post-recommender wrapper than as another
  input to rule-merging.
- Vision is the most expensive call (~10× a Haiku specialist). Running it
  AFTER tier-1 + the recommender means we only pay it for entries that
  already cleared the cheaper checks.

Public surface:
    apply_vision_check(verdict, *, df_4h, df_1h, df_15m, entry, sl, tp,
                       direction, vision_client) -> AgentVerdict

Returns a new ``AgentVerdict`` with vision telemetry attached. When
``vision_client`` is ``None`` or the toggle is off, returns the input
verdict unchanged.

Failure semantics: ANY error path (chart render, API error, cost-cap
hit, malformed JSON, unknown verdict) surfaces as a failed
``VisionEntryVerdict`` on the output. The enforced verdict is left at
its pre-vision value — vision NEVER takes down the entry decision.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Final

import pandas as pd

from agents.sdk_client import ImagePart, VisionClient, VisionClientError
from agents.types import VERDICT_RANK, AgentVerdict, Verdict, VisionEntryVerdict
from agents.vision_config import vision_check_enabled, vision_override_is_dry_run
from analysis.chart_renderer import TradeLevels, render_composite_chart
from analysis.layers import Direction

VALID_VERDICTS: Final[set[str]] = {"ENTER", "PLAN_OK", "WATCH", "PASS"}

VISION_ENTRY_SYSTEM_PROMPT: Final = """\
You are the VISION SECOND-OPINION layer of an ENTRY decision pipeline.
A deterministic 5-layer scorer + 5 text specialists have already produced
a tentative verdict (ENTER / PLAN_OK / WATCH / PASS) for a NEW trade
candidate. Your job is independent: look at the attached 4H + 1H + 15m
chart and decide whether the visual evidence supports the entry.

The chart shows three stacked timeframes, each with candles, MA25 (orange),
MA99 (blue), swing high (▼) / swing low (▲) markers, a volume sub-panel,
and an RSI(14) sub-panel. The 15m panel shows dashed Entry / SL / TP lines.

What to look for that structured features can MISS:
- Multi-candle visual structural breaks against the trade direction.
- Bearish/bullish DIVERGENCE between price and RSI across several swings.
- Decisive REJECTION wicks at visible historical S/R.
- Wedge / triangle / range exhaustion patterns wider than the feature
  window.
- An SL placement that is clearly inside a recent congestion zone, or a
  TP placement on the wrong side of obvious resistance.

VERDICT LADDER (most → least bullish):
    ENTER → PLAN_OK → WATCH → PASS

You may only suggest a verdict that is AS BULLISH AS or LESS BULLISH THAN
the text tier's verdict. The framework enforces downgrade-only at the
merge step regardless — stay aligned so your rationale is meaningful.

BIAS toward agreement unless the chart shows a clear visual contradiction.
When in doubt, agree with the text tier. Downgrade only on a SPECIFIC,
visible reason you can name in the rationale.

Output STRICT JSON only — no prose before or after, no markdown fences:

{
  "verdict": "ENTER" | "PLAN_OK" | "WATCH" | "PASS",
  "confidence": 1-10,
  "rationale": "1-3 sentences in Korean naming the visual evidence"
}
"""


def _build_user_content(
    *,
    symbol: str,
    direction: Direction,
    entry: float,
    sl: float,
    tp: float,
    text_verdict: Verdict,
    text_rationale: str,
) -> str:
    return (
        f"신규 진입 후보의 차트를 시각적으로 평가하고 JSON으로만 답하세요.\n\n"
        f"[Trade candidate]\n"
        f"symbol: {symbol}\n"
        f"direction: {direction}\n"
        f"entry: {entry:g}\n"
        f"stop_loss: {sl:g}\n"
        f"take_profit: {tp:g}\n\n"
        f"[Text-tier tentative verdict]\n"
        f"verdict: {text_verdict}\n"
        f"rationale: {text_rationale}\n"
    )


def _merge(text_verdict: Verdict, vision_verdict: Verdict) -> tuple[Verdict, bool]:
    """Downgrade-only merge — vision can only lower bullishness.

    Returns (final_verdict, overridden). ``overridden`` is True iff vision
    actually changed the enforced verdict."""
    if VERDICT_RANK[vision_verdict] < VERDICT_RANK[text_verdict]:
        return vision_verdict, True
    return text_verdict, False


def _run_vision(
    *,
    vision_client: VisionClient,
    text_verdict: Verdict,
    text_rationale: str,
    symbol: str,
    direction: Direction,
    entry: float,
    sl: float,
    tp: float,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> VisionEntryVerdict:
    """Render + call + parse. Catches every error path so callers can rely
    on always getting a ``VisionEntryVerdict`` back."""
    try:
        chart_b64 = render_composite_chart(
            df_4h, df_1h, df_15m,
            levels=TradeLevels(entry=entry, stop_loss=sl, take_profit=tp),
        )
    except Exception as e:  # noqa: BLE001
        return VisionEntryVerdict(
            verdict="PASS", confidence=0, rationale="",
            failed=True, failure_reason=f"chart render error: {e}",
        )

    user_content = _build_user_content(
        symbol=symbol, direction=direction,
        entry=entry, sl=sl, tp=tp,
        text_verdict=text_verdict, text_rationale=text_rationale,
    )
    try:
        body = vision_client.call_multimodal(
            system_prompt=VISION_ENTRY_SYSTEM_PROMPT,
            text_content=user_content,
            images=[ImagePart(data=chart_b64, media_type="image/png")],
        )
    except VisionClientError as e:
        return VisionEntryVerdict(
            verdict="PASS", confidence=0, rationale="",
            failed=True, failure_reason=str(e),
        )

    raw = body.get("verdict")
    if raw not in VALID_VERDICTS:
        return VisionEntryVerdict(
            verdict="PASS", confidence=0, rationale="",
            failed=True, failure_reason=f"invalid verdict: {raw!r}",
        )
    try:
        conf = int(body["confidence"])
    except (TypeError, ValueError, KeyError):
        return VisionEntryVerdict(
            verdict="PASS", confidence=0, rationale="",
            failed=True, failure_reason=f"confidence not int: {body.get('confidence')!r}",
        )
    return VisionEntryVerdict(
        verdict=raw,
        confidence=max(1, min(10, conf)),
        rationale=str(body.get("rationale", ""))[:500],
    )


def apply_vision_check(
    verdict: AgentVerdict,
    *,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    symbol: str,
    direction: Direction,
    entry: float,
    sl: float,
    tp: float,
    vision_client: VisionClient | None,
) -> AgentVerdict:
    """Run the vision pre-entry check and apply downgrade-only merge.

    Pass-through when no ``vision_client`` is supplied OR
    ``VISION_CHECK_DISABLED`` / ``VISION_DISABLED`` is set."""
    if vision_client is None or not vision_check_enabled():
        return verdict

    vision = _run_vision(
        vision_client=vision_client,
        text_verdict=verdict.verdict,
        text_rationale=verdict.rationale,
        symbol=symbol, direction=direction,
        entry=entry, sl=sl, tp=tp,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )

    dry_run = vision_override_is_dry_run()
    if vision.failed or dry_run:
        # Failed vision or dry-run: keep enforced verdict, record telemetry only.
        return replace(
            verdict,
            vision=vision,
            vision_overrode=False,
            vision_dry_run=dry_run,
        )

    final, overrode = _merge(verdict.verdict, vision.verdict)
    new_rationale = verdict.rationale
    if overrode:
        new_rationale = (
            f"{verdict.rationale}  [vision override → {vision.verdict}: "
            f"{vision.rationale}]"
        )[:700]
    return replace(
        verdict,
        verdict=final,
        rationale=new_rationale,
        vision=vision,
        vision_overrode=overrode,
        vision_dry_run=False,
    )
