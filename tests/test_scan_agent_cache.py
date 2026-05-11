"""Integration: scan.py only calls agents for setups that survive the
idempotency gate, and reuses cached specialist outputs within TTL.

The big-picture invariant we're testing: with a 2-min cron and 5 specialists
per setup, the previous code burned ~5 LLM calls per re-detected setup every
2 min. Now agent invocation is gated on both `state.already_alerted is False`
AND `SpecialistCache.get returns None`.
"""
from __future__ import annotations

import json

import pytest

import scan
from agents.specialist_cache import SpecialistCache
from agents.types import AgentVerdict, SpecialistOutput
from alert_state import AlertState
from analysis.layers import LayerResult, SetupEvaluation
from analysis.scanner_logic import SynthesizedSetup
from scan import EXIT_OK, ScanResult, _AgentInputs, run_scan
from scanner_config import (
    FileChannelConfig,
    NotificationsConfig,
    ScannerConfig,
    TelegramChannelConfig,
    ThresholdsConfig,
)


def _cfg(symbols=("BTCUSDT",), min_score=4) -> ScannerConfig:
    return ScannerConfig(
        symbols=tuple(symbols),
        thresholds=ThresholdsConfig(min_score=min_score, default_rr=3.0),
        notifications=NotificationsConfig(
            channels=("stdout",),
            file=FileChannelConfig(path="alerts.log"),
            telegram=TelegramChannelConfig(enabled=False),
        ),
    )


def _passing_eval() -> SetupEvaluation:
    return SetupEvaluation(
        layer_1=LayerResult(1, "pass", {"ma25": 1, "ma99": 0}),
        layer_2=LayerResult(1, "pass", {"closest_label": "x", "closest_price": 1, "distance_pct": 0.001}),
        layer_3=LayerResult(1, "pass", {"rejection_volume": 1}),
        layer_4=LayerResult(1, "pass", {"passed_swing_price": 79_900, "distance_pct": 0.001}),
        layer_5=LayerResult(1, "pass", {"rr": 3.0, "reward": 1, "risk": 1, "min_rr": 3.0}),
    )


def _result_for(symbol: str, sl_swing: float = 79_900.0) -> ScanResult:
    """Construct a passing ScanResult ready for the dispatch path."""
    import pandas as pd

    setup = SynthesizedSetup(
        direction="long", entry=80_000, sl=79_700, tp=80_900, sl_swing_price=sl_swing,
    )
    # Empty frames are fine — _attach_agent_verdict only forwards them to the
    # mocked runner, which we patch out below.
    return ScanResult(
        symbol=symbol, setup=setup, evaluation=_passing_eval(),
        agent_inputs=_AgentInputs(
            df_4h=pd.DataFrame(), df_1h=pd.DataFrame(),
            df_15m=pd.DataFrame(), df_1m=pd.DataFrame(),
        ),
    )


def _agent_response() -> tuple[AgentVerdict, list[SpecialistOutput]]:
    return (
        AgentVerdict(
            verdict="ENTER", confidence=78, rationale="agents concur",
            tier1_verdict="ENTER", downgraded_from_tier1=False,
        ),
        [
            SpecialistOutput(name="microstructure", findings={"x": 1}, confidence=8, rationale="r"),
            SpecialistOutput(name="trend_context", findings={"x": 1}, confidence=7, rationale="r"),
        ],
    )


def test_agents_not_called_when_setup_already_alerted(mocker, tmp_path):
    """Suppressed setups must NEVER trigger a runner call — the whole point
    of moving the invocation into the dispatch path."""
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    runner = mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    dispatch_spy = mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    state.record("confirmed:BTCUSDT", "confirmed:long", 79_900.0)
    cache = SpecialistCache(path=tmp_path / "cache.json")

    rc = run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert rc == EXIT_OK
    runner.assert_not_called()
    dispatch_spy.assert_not_called()
    # No cache entry written either — cache only stores on miss-then-fetch
    assert cache.entries == []


def test_agents_called_once_for_new_setup_and_cached(mocker, tmp_path):
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    runner = mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")

    rc = run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert rc == EXIT_OK
    runner.assert_called_once()
    # Cache populated with the (symbol, direction, sl_swing, L3 status) key
    hit = cache.get("BTCUSDT", "long", 79_900.0, "pass")
    assert hit is not None
    assert hit[0].verdict == "ENTER"


def test_cache_hit_reuses_verdict_without_calling_runner(mocker, tmp_path):
    """Second visit to the same setup under the SAME L3 status (e.g. alert
    state was reset) must hit cache and skip the LLM call entirely."""
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    runner = mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")
    verdict, specs = _agent_response()
    # _passing_eval has L3="pass" (confirmed-context); pre-populate cache
    # under the same status so the lookup hits.
    cache.put("BTCUSDT", "long", 79_900.0, "pass", verdict, specs)

    rc = run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert rc == EXIT_OK
    runner.assert_not_called()


def test_forming_cache_does_not_serve_confirmed_request(mocker, tmp_path):
    """When a setup was previously cached under FORMING context (L3=fail)
    and now presents as CONFIRMED (L3=pass), the cache must NOT serve the
    stale forming rationale — fresh agent call required."""
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    runner = mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")
    # Forming-context cache entry (L3=fail) for same symbol/direction/swing.
    forming_verdict, forming_specs = _agent_response()
    cache.put("BTCUSDT", "long", 79_900.0, "fail", forming_verdict, forming_specs)

    rc = run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert rc == EXIT_OK
    # Despite a forming-context entry existing, the L3=pass lookup misses
    # and the runner is invoked fresh.
    runner.assert_called_once()
    # Now both entries coexist — forming (fail) and confirmed (pass).
    assert cache.get("BTCUSDT", "long", 79_900.0, "fail") is not None
    assert cache.get("BTCUSDT", "long", 79_900.0, "pass") is not None


def test_cache_hit_marks_jsonl_record(mocker, tmp_path, monkeypatch):
    """`agent_cache_hit=True` should propagate to the jsonl log so stats can
    separate fresh vs. reused verdicts."""
    monkeypatch.chdir(tmp_path)  # alerts.jsonl is relative to cwd
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")
    verdict, specs = _agent_response()
    cache.put("BTCUSDT", "long", 79_900.0, "pass", verdict, specs)

    run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    jsonl = tmp_path / "alerts.jsonl"
    assert jsonl.exists()
    lines = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["agent_cache_hit"] is True
    assert lines[0]["agent_verdict"] == "ENTER"


def test_cache_miss_records_false_on_jsonl(mocker, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")

    run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    lines = [json.loads(l) for l in (tmp_path / "alerts.jsonl").read_text().splitlines()]
    assert lines[0]["agent_cache_hit"] is False


def test_runner_failure_does_not_block_dispatch(mocker, tmp_path):
    """Agent exceptions must not stop the alert from going out — the contract
    matches the previous in-scan_symbol behavior."""
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        side_effect=RuntimeError("backend unavailable"),
    )
    dispatch_spy = mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")
    rc = run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert rc == EXIT_OK
    dispatch_spy.assert_called_once()
    # Failed runner should NOT have written a cache entry
    assert cache.entries == []


def test_runner_returns_none_skips_cache_write(mocker, tmp_path):
    """When the backend is disabled (`get_default_client` → None →
    runner returns None), no cache entry should be written either."""
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists", return_value=None,
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache = SpecialistCache(path=tmp_path / "cache.json")
    rc = run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert rc == EXIT_OK
    assert cache.entries == []


def test_cache_persisted_after_scan(mocker, tmp_path):
    """run_scan must call cache.save() so the next cron tick can read it."""
    mocker.patch.object(scan, "scan", return_value=[_result_for("BTCUSDT")])
    mocker.patch.object(
        scan, "run_agentic_analysis_with_specialists",
        return_value=_agent_response(),
    )
    mocker.patch.object(scan, "dispatch")

    state = AlertState(path=tmp_path / "state.json")
    cache_path = tmp_path / "cache.json"
    cache = SpecialistCache(path=cache_path)
    run_scan(_cfg(), state=state, agent_cache=cache, quiet=True)
    assert cache_path.exists()
    reloaded = SpecialistCache.load(cache_path)
    # _passing_eval sets L3="pass", so the cache entry is stored under that key.
    assert reloaded.get("BTCUSDT", "long", 79_900.0, "pass") is not None
