"""Run the full agentic analysis tier on top of a Tier 1 setup.

Public entry point: `run_agentic_analysis(...) → AgentVerdict | None`.

5 specialists run in PARALLEL via ThreadPoolExecutor — sequential would be
~50 sec total (5 × ~10 sec each via CLI), parallel is ~15 sec (limited by
slowest single call). Critical for staying under 1-min cron budget.

If a specialist's input data (e.g., futures funding) fails to fetch, that
specialist gets `failed=True` while others continue. Graceful degradation.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from agents import (
    macro,
    memory,
    meta_judge,
    microstructure,
    recommender,
    risk,
    trend_context,
    volume_regime,
)
from agents.backend import get_default_client
from agents.types import AgentVerdict, SpecialistOutput, Verdict
from analysis.layers import Direction, SetupEvaluation
from data.binance import (
    BinanceError,
    fetch_funding_rate,
    fetch_klines,
    fetch_open_interest_hist,
)


def tier1_verdict_from_evaluation(ev: SetupEvaluation) -> Verdict:
    """Mirror the deterministic formatter's verdict logic (output/formatter.py)."""
    score = ev.total_score
    if score < 4:
        return "PASS"
    if score == 5:
        return "ENTER"
    l3 = ev.layer_3.status
    if l3 == "pass":
        return "ENTER"
    if l3 == "pending":
        return "PLAN_OK"
    return "WATCH"


def _safe_fetch_daily(symbol: str) -> pd.DataFrame | None:
    try:
        return fetch_klines(symbol, "1d", limit=100)
    except BinanceError:
        return None


def _safe_fetch_funding(symbol: str) -> list[dict] | None:
    try:
        return fetch_funding_rate(symbol, limit=8)
    except BinanceError:
        return None


def _safe_fetch_oi(symbol: str) -> list[dict] | None:
    try:
        return fetch_open_interest_hist(symbol, period="1h", limit=12)
    except BinanceError:
        return None


def _macro_specialist_with_data_fetch(
    client: Any, symbol: str, direction: Direction
) -> SpecialistOutput:
    """Fetch funding + OI inside specialist (since main loop doesn't have them)."""
    funding = _safe_fetch_funding(symbol)
    oi = _safe_fetch_oi(symbol)
    if funding is None or oi is None:
        return SpecialistOutput(
            name=macro.NAME,
            failed=True,
            failure_reason="failed to fetch funding/OI data",
        )
    return macro.evaluate(
        client, funding_history=funding, oi_history=oi, direction=direction,
    )


def run_agentic_analysis(
    ev: SetupEvaluation,
    *,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    symbol: str,
    entry: float,
    sl: float,
    tp: float,
    direction: Direction,
    client: Any | None = None,
) -> AgentVerdict | None:
    """Run the agentic tier. Returns AgentVerdict only (compat wrapper).

    For full specialist outputs as well, use run_agentic_analysis_with_specialists.
    """
    result = run_agentic_analysis_with_specialists(
        ev, df_15m=df_15m, df_1m=df_1m, df_4h=df_4h, df_1h=df_1h,
        symbol=symbol, entry=entry, sl=sl, tp=tp, direction=direction,
        client=client,
    )
    return result[0] if result else None


def run_agentic_analysis_with_specialists(
    ev: SetupEvaluation,
    *,
    df_15m: pd.DataFrame,
    df_1m: pd.DataFrame,
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    symbol: str,
    entry: float,
    sl: float,
    tp: float,
    direction: Direction,
    client: Any | None = None,
) -> tuple[AgentVerdict, list[SpecialistOutput]] | None:
    """Run the agentic tier. Returns (AgentVerdict, list[SpecialistOutput]) for
    rich display, or None when no backend available."""
    if client is None:
        client = get_default_client()
    if client is None:
        return None

    tier1 = tier1_verdict_from_evaluation(ev)
    df_daily = _safe_fetch_daily(symbol)

    # 5 specialists in parallel. Each is independent — failure of one
    # doesn't block others.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            "microstructure": pool.submit(
                microstructure.evaluate,
                client,
                df_15m=df_15m, df_1m=df_1m,
                entry=entry, direction=direction,
                layer_3_status=ev.layer_3.status,
                layer_3_reason=ev.layer_3.detail.get("reason"),
            ),
            "trend_context": pool.submit(
                trend_context.evaluate,
                client,
                df_1h=df_1h, df_4h=df_4h, df_daily=df_daily,
                direction=direction,
            ),
            "volume_regime": pool.submit(
                volume_regime.evaluate,
                client,
                df_15m=df_15m, df_1h=df_1h,
                direction=direction,
            ),
            "risk": pool.submit(
                risk.evaluate,
                client,
                entry=entry, sl=sl, tp=tp, direction=direction,
                df_1h=df_1h, df_15m=df_15m,
            ),
            "macro": pool.submit(
                _macro_specialist_with_data_fetch,
                client, symbol, direction,
            ),
        }
        specialists: list[SpecialistOutput] = []
        for name, fut in futures.items():
            try:
                specialists.append(fut.result(timeout=120))
            except Exception as e:
                specialists.append(
                    SpecialistOutput(
                        name=name, failed=True,
                        failure_reason=f"{type(e).__name__}: {e}",
                    )
                )

    # Diagnostic: log every specialist's failure reason to stderr so cron-env
    # issues (keychain lock, network blip, etc.) are visible in scan.log.
    for s in specialists:
        if s.failed:
            print(
                f"[agentic] {s.name} failed: {s.failure_reason}",
                file=sys.stderr,
            )

    meta = meta_judge.judge(specialists)
    verdict = recommender.recommend(
        tier1_verdict=tier1,
        specialists=specialists,
        meta=meta,
    )

    # Record to market memory for cross-run continuity. Best-effort —
    # any IO error is logged but doesn't fail the verdict.
    try:
        memory.record_entry(
            symbol=symbol,
            tier1_verdict=tier1,
            agent_verdict=verdict.verdict,
            confidence=verdict.confidence,
            note=verdict.rationale[:150],
        )
    except Exception as e:
        print(f"[agentic] memory record failed: {e}", file=sys.stderr)

    return verdict, specialists
