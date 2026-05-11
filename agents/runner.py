"""Run the full agentic analysis tier on top of a Tier 1 setup.

Public entry point: `run_agentic_analysis(...) → AgentVerdict | None`.
Returns None if the API client is unavailable (no ANTHROPIC_API_KEY) — caller
should fall back to Tier 1 alone.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from agents import meta_judge, microstructure, recommender, stub_specialists
from agents.backend import get_default_client
from agents.types import AgentVerdict, SpecialistOutput, Verdict
from analysis.layers import Direction, SetupEvaluation


def tier1_verdict_from_evaluation(ev: SetupEvaluation) -> Verdict:
    """Mirror the deterministic formatter's verdict logic (output/formatter.py).

    Kept here so the recommender's input is the same verdict the user sees on
    the dispatched alert. Single source of truth would be nicer; consider
    consolidating in a future refactor.
    """
    score = ev.total_score
    if score < 4:
        return "PASS"
    if score == 5:
        return "ENTER"
    # score == 4
    l3 = ev.layer_3.status
    if l3 == "pass":
        return "ENTER"
    if l3 == "pending":
        return "PLAN_OK"
    return "WATCH"


def run_agentic_analysis(
    ev: SetupEvaluation,
    *,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame,
    entry: float,
    direction: Direction,
    client: Any | None = None,
) -> AgentVerdict | None:
    """Run the agentic tier. Returns None when disabled (no backend available)."""
    if client is None:
        client = get_default_client()
    if client is None:
        return None

    tier1 = tier1_verdict_from_evaluation(ev)

    # Run specialists. Microstructure is real; others are stubs for now.
    specialists: list[SpecialistOutput] = [
        microstructure.evaluate(
            client,
            df_15m=df_15m,
            df_1m=df_1m,
            entry=entry,
            direction=direction,
            layer_3_status=ev.layer_3.status,
            layer_3_reason=ev.layer_3.detail.get("reason"),
        ),
        stub_specialists.trend_context_stub(),
        stub_specialists.volume_regime_stub(),
        stub_specialists.risk_stub(),
        stub_specialists.macro_stub(),
    ]

    meta = meta_judge.judge(specialists)
    return recommender.recommend(
        tier1_verdict=tier1,
        specialists=specialists,
        meta=meta,
    )
