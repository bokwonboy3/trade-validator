"""Meta-Judge — cross-validates specialist outputs before they reach the recommender.

Pure function. Inputs are SpecialistOutput list; output is MetaJudgeOutput.

Detects:
- specialists that failed
- contradictions: micro vs volume, trend vs macro, risk vs aggressive setup
- hallucination risk: many failures, conflicting bullish/bearish signals
"""
from __future__ import annotations

from agents.types import MetaJudgeOutput, SpecialistOutput


def judge(specialists: list[SpecialistOutput]) -> MetaJudgeOutput:
    failed = [s.name for s in specialists if s.failed]
    n_failed = len(failed)
    if n_failed == 0:
        risk = "low"
    elif n_failed <= 2:
        risk = "medium"
    else:
        risk = "high"

    contradictions: list[str] = []

    micro = _find(specialists, "microstructure")
    macro = _find(specialists, "macro")
    trend = _find(specialists, "trend_context")
    volume = _find(specialists, "volume_regime")
    risk_spec = _find(specialists, "risk")

    # Contradiction 1: micro says clean rejection but volume says no accumulation
    if _ok(micro) and _ok(volume):
        rq = micro.findings.get("rejection_quality")
        regime = volume.findings.get("regime")
        if rq == "clean" and regime == "neutral":
            contradictions.append(
                "micro clean rejection이지만 volume regime neutral — 거부 강도 의심"
            )
        if rq == "clean" and regime == "distribution":
            contradictions.append(
                "micro clean LONG rejection이지만 volume distribution — 모순"
            )

    # Contradiction 2: trend strong but macro opposite
    if _ok(trend) and _ok(macro):
        ts = trend.findings.get("trend_strength")
        mb = macro.findings.get("macro_bias")
        if isinstance(ts, (int, float)) and ts >= 7 and mb == "bearish":
            contradictions.append(
                f"trend_context strength {ts}/10 (강한 추세)이지만 macro bearish"
            )
        elif isinstance(ts, (int, float)) and ts <= 3 and mb == "bullish":
            contradictions.append(
                f"trend_context strength {ts}/10 (약한 추세)이지만 macro bullish"
            )

    # Contradiction 3: trend quality regime_change
    if _ok(trend):
        tq = trend.findings.get("trend_quality")
        if tq == "regime_change":
            contradictions.append("trend_context: regime change 감지 — Tier 1 신뢰도 낮음")

    # Contradiction 4: risk says tp_realism unlikely
    if _ok(risk_spec):
        tpr = risk_spec.findings.get("tp_realism")
        if tpr == "unlikely":
            contradictions.append(
                "risk: TP 도달 unlikely — R:R 3.0이라도 실제 reachable 아닐 수 있음"
            )

    # Contradiction 5: macro 'unusual_events' 가 trade 방향과 충돌
    if _ok(macro):
        events = macro.findings.get("unusual_events") or []
        if isinstance(events, list) and len(events) >= 2:
            contradictions.append(f"macro: 비정상 이벤트 {len(events)}개 — {events[:2]}")

    consistent = len(contradictions) == 0
    if contradictions and risk == "low":
        risk = "medium"

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


def _ok(s: SpecialistOutput | None) -> bool:
    return s is not None and not s.failed
