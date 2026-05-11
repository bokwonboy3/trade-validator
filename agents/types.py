"""Shared types for the agentic analysis tier.

Design principles (locked-in from architecture discussion):

1. **Tier 1 (deterministic) is sacred.** Agents NEVER modify the score or
   per-layer status produced by `analysis.layers.evaluate_setup`.

2. **Downgrade-only.** The final agent verdict can be at most as bullish as
   Tier 1's. Specifically:
       Tier 1 PASS    → agent verdict must be PASS
       Tier 1 WATCH   → agent verdict ∈ {WATCH, PASS}
       Tier 1 PLAN_OK → agent verdict ∈ {PLAN_OK, WATCH, PASS}
       Tier 1 ENTER   → agent verdict ∈ {ENTER, WATCH, PLAN_OK, PASS}
   Enforced in `agents.recommender.enforce_downgrade_only`.

3. **Graceful degradation.** Any agent call that fails returns a
   ``SpecialistError`` result instead of raising. The recommender treats
   missing specialists as "no contribution" rather than failing the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# The four verdicts that match the deterministic formatter output exactly.
# Match values in output/formatter.py::_recommendation.
Verdict = Literal["ENTER", "PLAN_OK", "WATCH", "PASS"]

VERDICT_RANK: dict[Verdict, int] = {
    "PASS": 0,
    "WATCH": 1,
    "PLAN_OK": 2,
    "ENTER": 3,
}


@dataclass(frozen=True)
class SpecialistOutput:
    """Result from one specialist agent. Structured (not free text)."""

    name: str  # e.g. "microstructure", "trend_context"
    # The specialist's domain-specific assessment as a small JSON-like dict.
    # Each specialist's prompt enforces a known schema; consumers must check
    # `failed` first.
    findings: dict[str, Any] = field(default_factory=dict)
    # 1~10 confidence the specialist has in its own findings.
    confidence: int = 0
    # 1~3 sentence rationale in natural language.
    rationale: str = ""
    # Set when the specialist couldn't run (network error, schema mismatch,
    # API timeout). Findings should be empty in this case.
    failed: bool = False
    failure_reason: str = ""


@dataclass(frozen=True)
class MetaJudgeOutput:
    """Cross-checks specialist outputs for consistency before recommendation."""

    consistent: bool = True
    contradictions: list[str] = field(default_factory=list)
    hallucination_risk: Literal["low", "medium", "high"] = "low"
    specialists_failed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentVerdict:
    """The final agentic verdict, ALWAYS bound by Tier 1 (downgrade-only)."""

    verdict: Verdict
    confidence: int  # 0~100
    rationale: str
    tier1_verdict: Verdict  # for transparency / audit
    downgraded_from_tier1: bool = False
