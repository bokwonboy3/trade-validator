"""Integration tests: vision_client threading through run_agentic_analysis_*
(Phase 8c PR-3).

The 5 specialists + memory recording are mocked so we can focus on the
vision-check wire-through. Per-specialist contracts are already covered
in tests/test_agents_client.py, tests/test_agents_snapshot.py, etc.
"""
from __future__ import annotations

import pandas as pd
import pytest

from agents import runner
from agents.types import AgentVerdict, MetaJudgeOutput, SpecialistOutput
from analysis.layers import LayerResult, SetupEvaluation


def _ev_enter() -> SetupEvaluation:
    """5/5 score with all layers pass → tier1 verdict ENTER."""
    p = LayerResult(score=1, status="pass", detail={})
    return SetupEvaluation(
        layer_1=p, layer_2=p, layer_3=p, layer_4=p, layer_5=p,
    )


def _df(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "openTime": list(range(n)),
            "open": [100.0] * n,
            "high": [100.5] * n,
            "low": [99.5] * n,
            "close": [100.0] * n,
            "volume": [5.0] * n,
            "closeTime": list(range(1, n + 1)),
        }
    )


@pytest.fixture
def stub_pipeline(mocker, monkeypatch):
    """Short-circuit specialists / meta-judge / recommender / memory IO.

    Sets up the runner so it returns a deterministic ENTER verdict, leaving
    only the vision_check integration as the thing under test.
    """
    monkeypatch.setenv("AGENT_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    # Make backend client construction succeed with a dummy object.
    mocker.patch.object(runner, "get_default_client", return_value=object())

    # ThreadPoolExecutor calls specialist.evaluate — short-circuit each one
    # to return a "failed" specialist so we don't need real API mocking.
    mocker.patch.object(
        runner.microstructure, "evaluate",
        return_value=SpecialistOutput(name="microstructure", failed=True,
                                      failure_reason="stub"),
    )
    mocker.patch.object(
        runner.trend_context, "evaluate",
        return_value=SpecialistOutput(name="trend_context", failed=True,
                                      failure_reason="stub"),
    )
    mocker.patch.object(
        runner.volume_regime, "evaluate",
        return_value=SpecialistOutput(name="volume_regime", failed=True,
                                      failure_reason="stub"),
    )
    mocker.patch.object(
        runner.risk, "evaluate",
        return_value=SpecialistOutput(name="risk", failed=True,
                                      failure_reason="stub"),
    )
    mocker.patch.object(
        runner, "_macro_specialist_with_data_fetch",
        return_value=SpecialistOutput(name="macro", failed=True,
                                      failure_reason="stub"),
    )
    mocker.patch.object(
        runner.meta_judge, "judge",
        return_value=MetaJudgeOutput(),
    )
    mocker.patch.object(
        runner.recommender, "recommend",
        return_value=AgentVerdict(
            verdict="ENTER", confidence=80, rationale="text-tier ok",
            tier1_verdict="ENTER",
        ),
    )
    mocker.patch.object(runner.memory, "record_entry")


def _run(vision_client=None):
    return runner.run_agentic_analysis_with_specialists(
        _ev_enter(),
        df_15m=_df(), df_1m=_df(), df_4h=_df(), df_1h=_df(),
        symbol="BTCUSDT",
        entry=100.0, sl=99.0, tp=103.0,
        direction="long",
        vision_client=vision_client,
    )


# --- no vision_client → vision_check is NOT invoked ----------------------


def test_no_vision_client_means_no_vision_check(stub_pipeline, mocker):
    apply_spy = mocker.patch(
        "agents.vision_check.apply_vision_check",
        side_effect=AssertionError("must not be called when vision_client is None"),
    )
    result = _run(vision_client=None)
    assert result is not None
    verdict, _ = result
    assert verdict.verdict == "ENTER"
    assert verdict.vision is None
    apply_spy.assert_not_called()


# --- vision_client provided → vision_check runs and can downgrade --------


def test_vision_client_threaded_through_and_can_downgrade(
    stub_pipeline, mocker, monkeypatch,
):
    monkeypatch.delenv("VISION_CHECK_DISABLED", raising=False)
    monkeypatch.delenv("VISION_DISABLED", raising=False)
    monkeypatch.delenv("VISION_OVERRIDE_DRY_RUN", raising=False)

    # Skip real chart render — chart_renderer has its own tests.
    from agents import vision_check as vc_mod
    mocker.patch.object(vc_mod, "render_composite_chart", return_value="FAKEPNG")

    class _VC:
        def call_multimodal(self, *, system_prompt, text_content, images, **_kw):
            return {
                "verdict": "WATCH",
                "confidence": 8,
                "rationale": "1H bearish divergence",
            }

    result = _run(vision_client=_VC())
    assert result is not None
    verdict, _ = result
    assert verdict.verdict == "WATCH"          # vision downgraded
    assert verdict.vision is not None
    assert verdict.vision.verdict == "WATCH"
    assert verdict.vision_overrode is True


# --- vision_check exception does NOT kill the run ------------------------


def test_vision_check_internal_exception_is_swallowed(stub_pipeline, mocker):
    # Force apply_vision_check itself to blow up — the runner must still
    # return the text-tier verdict so dispatch isn't broken.
    mocker.patch(
        "agents.vision_check.apply_vision_check",
        side_effect=RuntimeError("unexpected"),
    )
    sentinel = object()
    result = _run(vision_client=sentinel)
    assert result is not None
    verdict, _ = result
    assert verdict.verdict == "ENTER"
    assert verdict.vision is None
