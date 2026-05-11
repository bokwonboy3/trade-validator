"""Tests for the downgrade-only invariant + verdict ranking."""
from __future__ import annotations

import pytest

from agents.recommender import enforce_downgrade_only, recommend
from agents.types import (
    VERDICT_RANK,
    MetaJudgeOutput,
    SpecialistOutput,
)


def _spec(name: str, **kw) -> SpecialistOutput:
    return SpecialistOutput(name=name, **kw)


# --- VERDICT_RANK invariants ---
def test_verdict_rank_ordering():
    assert VERDICT_RANK["PASS"] < VERDICT_RANK["WATCH"]
    assert VERDICT_RANK["WATCH"] < VERDICT_RANK["PLAN_OK"]
    assert VERDICT_RANK["PLAN_OK"] < VERDICT_RANK["ENTER"]


# --- enforce_downgrade_only ---
def test_enforce_caps_upgrade_attempts():
    # Even if proposed > tier1, returned == tier1
    assert enforce_downgrade_only("PASS", "ENTER") == "PASS"
    assert enforce_downgrade_only("WATCH", "ENTER") == "WATCH"
    assert enforce_downgrade_only("WATCH", "PLAN_OK") == "WATCH"


def test_enforce_passes_through_valid_downgrade():
    assert enforce_downgrade_only("ENTER", "WATCH") == "WATCH"
    assert enforce_downgrade_only("ENTER", "PASS") == "PASS"
    assert enforce_downgrade_only("PLAN_OK", "WATCH") == "WATCH"


def test_enforce_passes_through_same_rank():
    assert enforce_downgrade_only("ENTER", "ENTER") == "ENTER"
    assert enforce_downgrade_only("PASS", "PASS") == "PASS"


# --- recommend() with various specialist combinations ---
def test_recommend_no_specialists_succeed_keeps_tier1_low_confidence():
    """All specialists failed → no downgrade, but confidence low."""
    failed_specs = [_spec(n, failed=True, failure_reason="x") for n in (
        "microstructure", "trend_context", "volume_regime", "risk", "macro"
    )]
    meta = MetaJudgeOutput(
        consistent=True,
        specialists_failed=[s.name for s in failed_specs],
        hallucination_risk="high",
    )
    v = recommend(tier1_verdict="ENTER", specialists=failed_specs, meta=meta)
    # high risk → one downgrade
    assert v.verdict == "PLAN_OK"
    assert v.downgraded_from_tier1 is True
    assert v.confidence <= 40


def test_recommend_microstructure_weak_rejection_downgrades_enter():
    """When Tier 1 says ENTER but micro says weak rejection → WATCH."""
    micro = _spec(
        "microstructure",
        findings={
            "rejection_quality": "weak",
            "forming_progress": "n/a",
            "false_positive_risk": "low",
        },
        confidence=7,
        rationale="wick is small relative to body",
    )
    others = [_spec(n, findings={}, confidence=5, rationale="ok") for n in (
        "trend_context", "volume_regime", "risk", "macro"
    )]
    meta = MetaJudgeOutput(consistent=True, hallucination_risk="low")
    v = recommend(
        tier1_verdict="ENTER",
        specialists=[micro, *others],
        meta=meta,
    )
    assert v.verdict == "WATCH"
    assert v.downgraded_from_tier1 is True
    assert "거부 신호 약함" in v.rationale or "weak" in v.rationale


def test_recommend_clean_micro_keeps_enter():
    """Clean microstructure + no contradictions → Tier 1 preserved."""
    micro = _spec(
        "microstructure",
        findings={
            "rejection_quality": "clean",
            "forming_progress": "late",
            "false_positive_risk": "low",
        },
        confidence=9,
        rationale="hammer + volume spike",
    )
    others = [_spec(n, findings={}, confidence=7, rationale="ok") for n in (
        "trend_context", "volume_regime", "risk", "macro"
    )]
    meta = MetaJudgeOutput(consistent=True, hallucination_risk="low")
    v = recommend(
        tier1_verdict="ENTER",
        specialists=[micro, *others],
        meta=meta,
    )
    assert v.verdict == "ENTER"
    assert v.downgraded_from_tier1 is False


def test_recommend_cannot_upgrade_tier1_pass():
    """Even with perfect specialists, Tier 1 PASS stays PASS."""
    micro = _spec(
        "microstructure",
        findings={
            "rejection_quality": "clean",
            "forming_progress": "late",
            "false_positive_risk": "low",
        },
        confidence=10,
        rationale="very strong",
    )
    others = [_spec(n, findings={}, confidence=10, rationale="strong") for n in (
        "trend_context", "volume_regime", "risk", "macro"
    )]
    meta = MetaJudgeOutput(consistent=True, hallucination_risk="low")
    v = recommend(
        tier1_verdict="PASS",
        specialists=[micro, *others],
        meta=meta,
    )
    # No upgrade allowed
    assert v.verdict == "PASS"
    assert v.downgraded_from_tier1 is False


def test_recommend_false_positive_high_downgrades_to_watch():
    micro = _spec(
        "microstructure",
        findings={
            "rejection_quality": "clean",
            "forming_progress": "late",
            "false_positive_risk": "high",
        },
        confidence=6,
        rationale="thin liquidity",
    )
    others = [_spec(n, findings={}, confidence=5, rationale="ok") for n in (
        "trend_context", "volume_regime", "risk", "macro"
    )]
    meta = MetaJudgeOutput(consistent=True, hallucination_risk="low")
    v = recommend(
        tier1_verdict="ENTER",
        specialists=[micro, *others],
        meta=meta,
    )
    assert v.verdict == "WATCH"
