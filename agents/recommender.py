"""Action Recommender — synthesizes Tier 1 + specialists + meta-judge into a verdict.

**The single most important rule:** the recommender can only DOWNGRADE Tier 1's
verdict, never UPGRADE. This preserves the deterministic framework's discipline
("Score 4/5 미만 = 무조건 패스") while letting agents add caution where market
context warrants it.

```
  Tier 1     →  allowed agent verdicts
  ─────────────────────────────────
  PASS       →  PASS                       (no upgrade — cannot create entries)
  WATCH      →  WATCH, PASS                (only downgrade or hold)
  PLAN_OK    →  PLAN_OK, WATCH, PASS       (only downgrade or hold)
  ENTER      →  ENTER, PLAN_OK, WATCH, PASS  (any non-upgrade)
```

For now the recommender is rule-based (not LLM). When meta-judge flags risks,
the verdict is dialed back. Future: optional LLM call here for richer rationale.
"""
from __future__ import annotations

from agents.types import (
    VERDICT_RANK,
    AgentVerdict,
    MetaJudgeOutput,
    SpecialistOutput,
    Verdict,
)


def enforce_downgrade_only(tier1: Verdict, proposed: Verdict) -> Verdict:
    """Cap `proposed` at `tier1`'s rank. Returns whichever is more conservative."""
    if VERDICT_RANK[proposed] > VERDICT_RANK[tier1]:
        return tier1
    return proposed


def recommend(
    *,
    tier1_verdict: Verdict,
    specialists: list[SpecialistOutput],
    meta: MetaJudgeOutput,
) -> AgentVerdict:
    """Produce the final agent verdict given Tier 1 baseline + analysis.

    Rule-based for v1. The recommendation logic:
      1. Start with Tier 1 verdict.
      2. If meta-judge says high hallucination risk → downgrade one step.
      3. If specialists flag specific concerns → downgrade per-concern.
      4. Final: cap at Tier 1 (downgrade-only invariant).
    """
    proposed: Verdict = tier1_verdict
    rationale_parts: list[str] = []

    # Microstructure concern: false-positive risk or weak rejection
    micro = _find(specialists, "microstructure")
    if micro and not micro.failed:
        rq = micro.findings.get("rejection_quality")
        fpr = micro.findings.get("false_positive_risk")
        if rq in ("weak", "false_positive_risk") and tier1_verdict == "ENTER":
            proposed = "WATCH"
            rationale_parts.append(
                f"microstructure: {rq} (거부 신호 약함) → WATCH"
            )
        if fpr == "high" and tier1_verdict in ("ENTER", "PLAN_OK"):
            proposed = "WATCH"
            rationale_parts.append("false-positive 위험 높음 → WATCH")

    # Risk specialist: tp_realism unlikely → downgrade
    risk_spec = _find(specialists, "risk")
    if risk_spec and not risk_spec.failed:
        tpr = risk_spec.findings.get("tp_realism")
        slq = risk_spec.findings.get("sl_quality")
        if tpr == "unlikely" and tier1_verdict in ("ENTER", "PLAN_OK"):
            proposed = _downgrade_one(proposed)
            rationale_parts.append("risk: TP unlikely → 보수화")
        if slq == "tight" and tier1_verdict == "ENTER":
            proposed = _downgrade_one(proposed)
            rationale_parts.append("risk: SL이 ATR 대비 tight → 보수화")

    # Trend context: regime_change or weak strength
    trend = _find(specialists, "trend_context")
    if trend and not trend.failed:
        tq = trend.findings.get("trend_quality")
        ts = trend.findings.get("trend_strength")
        if tq == "regime_change":
            proposed = _downgrade_one(proposed)
            rationale_parts.append("trend: regime change 감지")
        elif isinstance(ts, (int, float)) and ts <= 3 and tier1_verdict == "ENTER":
            proposed = _downgrade_one(proposed)
            rationale_parts.append(f"trend strength {ts}/10 (약함)")

    # Volume regime: distribution contra long, accumulation contra short
    # (no direction in recommender — let the contradictions list capture mismatches)

    # Macro contradiction (보수적 — strong opposing macro만)
    macro = _find(specialists, "macro")
    if macro and not macro.failed:
        bias = macro.findings.get("macro_bias", "neutral")
        events = macro.findings.get("unusual_events") or []
        if isinstance(events, list) and len(events) >= 2 and tier1_verdict == "ENTER":
            proposed = _downgrade_one(proposed)
            rationale_parts.append(f"macro: 비정상 이벤트 {len(events)}개")

    # Meta-judge: high hallucination risk → blanket downgrade
    if meta.hallucination_risk == "high":
        proposed = _downgrade_one(proposed)
        rationale_parts.append(
            f"meta-judge: 위험 high ({len(meta.specialists_failed)}명 실패)"
        )

    # Hard cap: never upgrade beyond Tier 1
    final = enforce_downgrade_only(tier1_verdict, proposed)
    downgraded = VERDICT_RANK[final] < VERDICT_RANK[tier1_verdict]

    if not rationale_parts:
        rationale_parts.append("specialists 분석 결과 Tier 1 유지")
    rationale = " | ".join(rationale_parts)

    # Confidence: average of specialist confidences (1-10 → 0-100), downgrade penalty
    valid = [s for s in specialists if not s.failed]
    if valid:
        avg_conf = sum(s.confidence for s in valid) / len(valid)
        confidence = int(avg_conf * 10)  # 1-10 → 10-100
    else:
        confidence = 30  # no specialists succeeded → low confidence
    if meta.hallucination_risk == "high":
        confidence = min(confidence, 40)
    elif meta.hallucination_risk == "medium":
        confidence = min(confidence, 70)

    return AgentVerdict(
        verdict=final,
        confidence=confidence,
        rationale=rationale,
        tier1_verdict=tier1_verdict,
        downgraded_from_tier1=downgraded,
    )


def _find(specialists: list[SpecialistOutput], name: str) -> SpecialistOutput | None:
    for s in specialists:
        if s.name == name:
            return s
    return None


def _downgrade_one(v: Verdict) -> Verdict:
    """Move one rank down in the verdict ladder."""
    order: list[Verdict] = ["PASS", "WATCH", "PLAN_OK", "ENTER"]
    idx = order.index(v)
    if idx == 0:
        return v
    return order[idx - 1]
