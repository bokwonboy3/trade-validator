"""Tests for the Anthropic client wrapper + specialist base + runner.

All Anthropic calls are mocked — no live API hits.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from agents import microstructure
from agents.client import (
    ANTHROPIC_API_KEY_ENV,
    AgentClient,
    AgentClientError,
    get_default_client_or_none,
)
from agents.runner import run_agentic_analysis, tier1_verdict_from_evaluation
from agents.specialist_base import run_specialist
from agents.types import SpecialistOutput
from analysis.layers import LayerResult, SetupEvaluation


# --- Client construction ---
def test_client_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    with pytest.raises(AgentClientError, match="ANTHROPIC_API_KEY"):
        AgentClient()


def test_get_default_client_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    assert get_default_client_or_none() is None


def test_get_default_client_constructs_when_set(monkeypatch):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = get_default_client_or_none()
    assert c is not None


# --- call_structured: JSON parsing + fence stripping ---
class _FakeText:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, body: str):
        self.content = [_FakeText(body)]


def _patch_create(mocker, body: str):
    mocker.patch.object(
        AgentClient,
        "_client",
        new=type(
            "FakeClient",
            (),
            {
                "messages": type(
                    "M",
                    (),
                    {"create": staticmethod(lambda **kw: _FakeMessage(body))},
                )()
            },
        )(),
    )


def test_call_structured_strips_markdown_fence(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = AgentClient()
    fenced = '```json\n{"verdict": "ENTER", "confidence": 8}\n```'
    mocker.patch.object(
        c, "_client", mocker.Mock(messages=mocker.Mock(create=lambda **kw: _FakeMessage(fenced)))
    )
    body = c.call_structured(system_prompt="sys", user_content="msg")
    assert body == {"verdict": "ENTER", "confidence": 8}


def test_call_structured_raises_on_invalid_json(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = AgentClient()
    mocker.patch.object(
        c, "_client", mocker.Mock(messages=mocker.Mock(create=lambda **kw: _FakeMessage("not json")))
    )
    with pytest.raises(AgentClientError, match="not valid JSON"):
        c.call_structured(system_prompt="sys", user_content="msg")


# --- run_specialist: graceful failure ---
def test_run_specialist_graceful_on_api_error(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = AgentClient()
    mocker.patch.object(c, "call_structured", side_effect=AgentClientError("network down"))
    out = run_specialist(
        client=c,
        name="test",
        system_prompt="sys",
        user_content="msg",
        expected_keys=["x"],
    )
    assert out.failed is True
    assert "network down" in out.failure_reason


def test_run_specialist_graceful_on_missing_keys(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = AgentClient()
    mocker.patch.object(c, "call_structured", return_value={"only_this": "field"})
    out = run_specialist(
        client=c,
        name="test",
        system_prompt="sys",
        user_content="msg",
        expected_keys=["x"],
    )
    assert out.failed is True
    assert "missing keys" in out.failure_reason


def test_run_specialist_happy_path(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = AgentClient()
    mocker.patch.object(
        c, "call_structured",
        return_value={
            "x": "value",
            "confidence": 8,
            "rationale": "looks fine",
        },
    )
    out = run_specialist(
        client=c,
        name="test",
        system_prompt="sys",
        user_content="msg",
        expected_keys=["x"],
    )
    assert out.failed is False
    assert out.findings == {"x": "value"}
    assert out.confidence == 8
    assert out.rationale == "looks fine"


# --- Microstructure user-content shape ---
def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_microstructure_build_user_content_includes_candles():
    df_15m = _df([{
        "openTime": 1_700_000_000_000 + i * 900_000,
        "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "volume": 50.0,
    } for i in range(10)])
    df_1m = _df([{
        "openTime": 1_700_000_000_000 + i * 60_000,
        "open": 100.0, "high": 100.1, "low": 99.9,
        "close": 100.0, "volume": 5.0,
    } for i in range(20)])
    content = microstructure.build_user_content(
        df_15m=df_15m, df_1m=df_1m, entry=100.0,
        direction="long", layer_3_status="fail",
        layer_3_reason="no_rejection_with_volume_in_touch_window",
    )
    assert "long" in content
    assert "100.0" in content
    assert "candles_15m_last_8" in content
    assert "candles_1m_last_20" in content
    assert "no_rejection" in content


# --- tier1_verdict_from_evaluation ---
def _ev(score: int, l3_status: str = "fail") -> SetupEvaluation:
    """Build a SetupEvaluation with exact `score` and a specific Layer 3 status.

    When `l3_status` is not "pass", Layer 3 contributes 0 — the remaining
    layers (1, 2, 4, 5) carry the score.
    """
    p = LayerResult(score=1, status="pass", detail={})
    f = LayerResult(score=0, status="fail", detail={})
    l3 = LayerResult(score=1 if l3_status == "pass" else 0, status=l3_status, detail={})
    needed_passes = score - l3.score
    other_slots = [p, p, p, p]  # layers 1, 2, 4, 5
    for i in range(4):
        if i >= needed_passes:
            other_slots[i] = f
    return SetupEvaluation(
        layer_1=other_slots[0],
        layer_2=other_slots[1],
        layer_3=l3,
        layer_4=other_slots[2],
        layer_5=other_slots[3],
    )


def test_tier1_verdict_below_threshold_is_pass():
    assert tier1_verdict_from_evaluation(_ev(3)) == "PASS"


def test_tier1_verdict_5_of_5_is_enter():
    assert tier1_verdict_from_evaluation(_ev(5, l3_status="pass")) == "ENTER"


def test_tier1_verdict_4_of_5_l3_fail_is_watch():
    assert tier1_verdict_from_evaluation(_ev(4, l3_status="fail")) == "WATCH"


def test_tier1_verdict_4_of_5_l3_pending_is_plan_ok():
    assert tier1_verdict_from_evaluation(_ev(4, l3_status="pending")) == "PLAN_OK"


# --- run_agentic_analysis returns None when API key absent ---
def test_run_agentic_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    ev = _ev(4, l3_status="fail")
    df = _df([{
        "openTime": 1_700_000_000_000 + i * 60_000,
        "open": 100.0, "high": 100.1, "low": 99.9,
        "close": 100.0, "volume": 5.0,
    } for i in range(20)])
    result = run_agentic_analysis(
        ev, df_15m=df, df_1m=df, entry=100.0, direction="long",
    )
    assert result is None  # graceful degradation
