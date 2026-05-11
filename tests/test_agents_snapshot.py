"""Snapshot tests for agent verdicts on known scenarios.

Why: LLM-backed specialists are non-deterministic. To detect *meaningful*
regression (vs noise), we lock the *verdict + confidence range + which
specialists flagged concerns* — not the exact rationale text.

How:
  - Each scenario = a fixture (df_15m, df_1m, df_4h, df_1h + setup)
  - Run agentic pipeline (with mocked LLM responses for reproducibility)
  - Compare against snapshot JSON
  - Update snapshots: `SNAPSHOT_UPDATE=1 pytest tests/test_agents_snapshot.py`

For live regression (real LLM): `pytest -m live --run-live tests/test_agents_snapshot.py`
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from agents.recommender import recommend
from agents.meta_judge import judge
from agents.types import MetaJudgeOutput, SpecialistOutput

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.json"


def _load_snapshot(name: str) -> dict | None:
    p = _snapshot_path(name)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _write_snapshot(name: str, data: dict) -> None:
    p = _snapshot_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _assert_snapshot(name: str, actual: dict) -> None:
    """Compare `actual` to saved snapshot. SNAPSHOT_UPDATE=1 to refresh."""
    if os.environ.get("SNAPSHOT_UPDATE") == "1":
        _write_snapshot(name, actual)
        return
    expected = _load_snapshot(name)
    if expected is None:
        _write_snapshot(name, actual)
        pytest.skip(f"snapshot {name} created fresh; re-run to validate")
    assert actual == expected, (
        f"snapshot drift: {name}\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"  to update: SNAPSHOT_UPDATE=1 pytest tests/test_agents_snapshot.py::{name}"
    )


def _spec(name: str, **kw) -> SpecialistOutput:
    return SpecialistOutput(name=name, **kw)


# --- Scenario 1: All clean → ENTER preserved ---
def test_snapshot_all_clean_enter_preserved():
    specs = [
        _spec("microstructure",
              findings={"rejection_quality": "clean", "forming_progress": "late", "false_positive_risk": "low"},
              confidence=9, rationale="ok"),
        _spec("trend_context",
              findings={"trend_strength": 8, "trend_quality": "clean", "concerns": []},
              confidence=8, rationale="ok"),
        _spec("volume_regime",
              findings={"regime": "accumulation", "unusual_activity": False, "comparison_to_avg": "1.4"},
              confidence=7, rationale="ok"),
        _spec("risk",
              findings={"sl_quality": "optimal", "tp_realism": "likely", "expected_hold_hours": 12},
              confidence=8, rationale="ok"),
        _spec("macro",
              findings={"macro_bias": "bullish", "unusual_events": []},
              confidence=7, rationale="ok"),
    ]
    meta = judge(specs)
    v = recommend(tier1_verdict="ENTER", specialists=specs, meta=meta)
    actual = {
        "verdict": v.verdict,
        "tier1": v.tier1_verdict,
        "downgraded": v.downgraded_from_tier1,
        "confidence_bucket": _bucket(v.confidence),
        "n_contradictions": len(meta.contradictions),
        "hallucination_risk": meta.hallucination_risk,
    }
    _assert_snapshot("all_clean_enter", actual)


# --- Scenario 2: Micro weak → downgrade ---
def test_snapshot_micro_weak_downgrades():
    specs = [
        _spec("microstructure",
              findings={"rejection_quality": "weak", "forming_progress": "early", "false_positive_risk": "medium"},
              confidence=4, rationale="thin wick"),
        _spec("trend_context",
              findings={"trend_strength": 7, "trend_quality": "clean", "concerns": []},
              confidence=7, rationale="ok"),
        _spec("volume_regime",
              findings={"regime": "neutral", "unusual_activity": False},
              confidence=6, rationale="ok"),
        _spec("risk",
              findings={"sl_quality": "optimal", "tp_realism": "likely", "expected_hold_hours": 8},
              confidence=8, rationale="ok"),
        _spec("macro",
              findings={"macro_bias": "neutral", "unusual_events": []},
              confidence=6, rationale="ok"),
    ]
    meta = judge(specs)
    v = recommend(tier1_verdict="ENTER", specialists=specs, meta=meta)
    actual = {
        "verdict": v.verdict,
        "downgraded": v.downgraded_from_tier1,
        "concern_in_rationale": "거부" in v.rationale or "weak" in v.rationale.lower(),
    }
    _assert_snapshot("micro_weak_downgrade", actual)


# --- Scenario 3: Risk unlikely → downgrade ---
def test_snapshot_risk_unlikely_downgrades():
    specs = [
        _spec("microstructure",
              findings={"rejection_quality": "clean", "forming_progress": "late", "false_positive_risk": "low"},
              confidence=8, rationale="ok"),
        _spec("trend_context",
              findings={"trend_strength": 7, "trend_quality": "clean", "concerns": []},
              confidence=7, rationale="ok"),
        _spec("volume_regime",
              findings={"regime": "accumulation", "unusual_activity": False},
              confidence=7, rationale="ok"),
        _spec("risk",
              findings={"sl_quality": "optimal", "tp_realism": "unlikely", "expected_hold_hours": 72},
              confidence=7, rationale="TP 너무 멀다"),
        _spec("macro",
              findings={"macro_bias": "neutral", "unusual_events": []},
              confidence=6, rationale="ok"),
    ]
    meta = judge(specs)
    v = recommend(tier1_verdict="ENTER", specialists=specs, meta=meta)
    actual = {
        "verdict": v.verdict,
        "downgraded": v.downgraded_from_tier1,
        "risk_concern": "TP" in v.rationale or "unlikely" in v.rationale.lower(),
    }
    _assert_snapshot("risk_unlikely_downgrade", actual)


# --- Scenario 4: All failed → high risk, downgrade ---
def test_snapshot_all_failed_high_risk():
    specs = [
        _spec(n, failed=True, failure_reason="network") for n in
        ("microstructure", "trend_context", "volume_regime", "risk", "macro")
    ]
    meta = judge(specs)
    v = recommend(tier1_verdict="ENTER", specialists=specs, meta=meta)
    actual = {
        "verdict": v.verdict,
        "downgraded": v.downgraded_from_tier1,
        "all_failed_handled": meta.hallucination_risk == "high",
        "confidence_low": v.confidence <= 40,
    }
    _assert_snapshot("all_failed_high_risk", actual)


# --- Helpers ---
def _bucket(n: int) -> str:
    if n < 40:
        return "low"
    if n < 65:
        return "medium"
    if n < 85:
        return "high"
    return "very_high"
