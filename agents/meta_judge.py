"""Meta-Judge — cross-validates specialist outputs before they reach the recommender.

Cheap, deterministic logic (no LLM call for now). Flags:
- specialists that failed
- obvious contradictions between specialists
- hallucination risk based on how many failed / disagreed

When the meta-judge flags high risk, the recommender becomes more conservative.
"""
from __future__ import annotations

from agents.types import MetaJudgeOutput, SpecialistOutput


def judge(specialists: list[SpecialistOutput]) -> MetaJudgeOutput:
    """Apply cross-checks. Pure function — easily testable."""
    failed = [s.name for s in specialists if s.failed]

    # Hallucination risk heuristic:
    # - 0 failures → low
    # - 1-2 failures → medium
    # - 3+ failures → high
    n_failed = len(failed)
    if n_failed == 0:
        risk = "low"
    elif n_failed <= 2:
        risk = "medium"
    else:
        risk = "high"

    # Contradiction detection — keep simple for now, expand as patterns emerge.
    contradictions: list[str] = []

    micro = _find(specialists, "microstructure")
    macro = _find(specialists, "macro")
    trend = _find(specialists, "trend_context")
    volume = _find(specialists, "volume_regime")

    # Example contradiction: micro says clean rejection but volume says
    # accumulation/distribution thinks otherwise.
    if micro and not micro.failed and volume and not volume.failed:
        rq = micro.findings.get("rejection_quality")
        regime = volume.findings.get("regime")
        if rq == "clean" and regime == "neutral":
            # Mild — clean rejection without volume confirmation is suspicious
            contradictions.append(
                "microstructure: clean rejection / volume_regime: neutral — "
                "거부가 강하다는데 거래량은 평범"
            )

    # Direction conflict: macro bearish but trend bullish, or vice-versa
    if trend and not trend.failed and macro and not macro.failed:
        ts = trend.findings.get("trend_strength", 5)
        mb = macro.findings.get("macro_bias", "neutral")
        if isinstance(ts, int) and ts >= 7 and mb == "bearish":
            contradictions.append(
                "trend_context: strong bull / macro: bearish — macro 부담"
            )
        elif isinstance(ts, int) and ts <= 3 and mb == "bullish":
            contradictions.append(
                "trend_context: weak / macro: bullish — 추세 약하지만 macro 우호"
            )

    consistent = len(contradictions) == 0
    if contradictions and risk == "low":
        risk = "medium"  # contradictions bump the risk

    return MetaJudgeOutput(
        consistent=consistent,
        contradictions=contradictions,
        hallucination_risk=risk,  # type: ignore[arg-type]
        specialists_failed=failed,
    )


def _find(specialists: list[SpecialistOutput], name: str) -> SpecialistOutput | None:
    for s in specialists:
        if s.name == name:
            return s
    return None
