"""Placeholder implementations for 4 specialists not yet fully implemented.

Each returns a deterministic ``SpecialistOutput`` so the rest of the pipeline
(meta-judge, recommender) can be tested end-to-end. Will be replaced by
real Anthropic-backed agents in follow-up commits.

Order of planned implementation:
1. Trend Context (Daily/Weekly MA + slope)
2. Volume Regime (accumulation vs distribution)
3. Risk (SL/TP appropriateness vs ATR)
4. Macro (funding rate, OI, news)
"""
from __future__ import annotations

from agents.types import SpecialistOutput


def trend_context_stub() -> SpecialistOutput:
    return SpecialistOutput(
        name="trend_context",
        findings={
            "trend_strength": 5,
            "trend_quality": "stub",
            "concerns": [],
        },
        confidence=5,
        rationale="[stub] Trend Context specialist not yet implemented.",
    )


def volume_regime_stub() -> SpecialistOutput:
    return SpecialistOutput(
        name="volume_regime",
        findings={
            "regime": "neutral",
            "unusual_activity": False,
        },
        confidence=5,
        rationale="[stub] Volume Regime specialist not yet implemented.",
    )


def risk_stub() -> SpecialistOutput:
    return SpecialistOutput(
        name="risk",
        findings={
            "sl_quality": "stub",
            "tp_realism": "stub",
            "expected_hold_hours": None,
        },
        confidence=5,
        rationale="[stub] Risk specialist not yet implemented.",
    )


def macro_stub() -> SpecialistOutput:
    return SpecialistOutput(
        name="macro",
        findings={
            "macro_bias": "neutral",
            "unusual_events": [],
        },
        confidence=5,
        rationale="[stub] Macro specialist not yet implemented.",
    )
