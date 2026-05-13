"""Tests for agents.position_advisor — LLM-based HOLD/TIGHTEN/PARTIAL/EXIT
evaluation of open trades. Mocks AgentClient to avoid real CLI calls."""
from __future__ import annotations

import pandas as pd
import pytest

from agents.position_advisor import VALID_ACTIONS, AdvisorOutput, evaluate


def _klines(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame({
        "openTime": list(range(n)),
        "open": [80000] * n,
        "high": [80100] * n,
        "low": [79900] * n,
        "close": [80050] * n,
        "volume": [1.0] * n,
        "closeTime": list(range(1, n + 1)),
    })


class _FakeClient:
    """Stub AgentClient that returns canned responses."""
    def __init__(self, response: dict):
        self.response = response
        self.last_user_content: str | None = None

    def call_structured(self, *, system_prompt, user_content, **kwargs):
        self.last_user_content = user_content
        return self.response


def _call(client) -> AdvisorOutput:
    return evaluate(
        symbol="BTCUSDT", direction="long",
        entry=80000.0, current_price=81000.0, pnl_pct=1.25,
        df_4h=_klines(), df_1h=_klines(), df_15m=_klines(),
        client=client,
    )


def test_evaluate_returns_hold_action():
    client = _FakeClient({
        "action": "HOLD",
        "confidence": 7,
        "rationale": "thesis intact, momentum cooling but not broken",
    })
    result = _call(client)
    assert result.action == "HOLD"
    assert result.confidence == 7
    assert "thesis" in result.rationale
    assert not result.failed


def test_evaluate_returns_exit_action():
    client = _FakeClient({
        "action": "EXIT",
        "confidence": 9,
        "rationale": "structural breakdown",
    })
    result = _call(client)
    assert result.action == "EXIT"
    assert result.confidence == 9


def test_evaluate_invalid_action_failed():
    client = _FakeClient({
        "action": "BANANA",
        "confidence": 5,
        "rationale": "x",
    })
    result = _call(client)
    assert result.failed
    assert "invalid action" in result.failure_reason


def test_evaluate_non_int_confidence_failed():
    client = _FakeClient({
        "action": "HOLD",
        "confidence": "high",
        "rationale": "x",
    })
    result = _call(client)
    assert result.failed
    assert "confidence" in result.failure_reason


def test_evaluate_clamps_confidence_to_1_10():
    client = _FakeClient({
        "action": "HOLD",
        "confidence": 99,  # absurdly high
        "rationale": "x",
    })
    result = _call(client)
    assert result.confidence == 10


def test_evaluate_truncates_rationale():
    client = _FakeClient({
        "action": "HOLD",
        "confidence": 5,
        "rationale": "x" * 1000,
    })
    result = _call(client)
    assert len(result.rationale) == 500


def test_no_client_returns_failed_output():
    """When no agent backend available (client=None) and get_default_client()
    returns None, should return failed=True."""
    import agents.position_advisor as pa
    # monkeypatch get_default_client to return None
    import unittest.mock as mock
    with mock.patch.object(pa, "get_default_client", return_value=None):
        result = evaluate(
            symbol="BTCUSDT", direction="long",
            entry=80000.0, current_price=81000.0, pnl_pct=1.25,
            df_4h=_klines(), df_1h=_klines(), df_15m=_klines(),
        )
    assert result.failed
    assert result.failure_reason == "no backend"


def test_valid_actions_set():
    """Sanity: VALID_ACTIONS matches the documented 4 actions."""
    assert VALID_ACTIONS == {"HOLD", "TIGHTEN", "PARTIAL", "EXIT"}


def test_user_content_includes_trade_context():
    """Verify the prompt body contains the trade/market info LLM needs."""
    client = _FakeClient({
        "action": "HOLD", "confidence": 5, "rationale": "x",
    })
    _call(client)
    assert client.last_user_content is not None
    body = client.last_user_content
    assert "BTCUSDT" in body
    assert "long" in body
    assert "80000" in body  # entry
    assert "81000" in body  # current
    assert "1.25" in body  # pnl_pct


def test_user_content_uses_semantic_features_not_raw_candle_dump():
    """Phase 8b: advisor sends pre-computed feature sections, NOT a 38-candle
    raw dump. Guard against regression — the old format used keys like
    'candles_4h_last_10'; the new format uses 'trend_4h', 'structure_1h', etc."""
    client = _FakeClient({
        "action": "HOLD", "confidence": 5, "rationale": "x",
    })
    _call(client)
    body = client.last_user_content
    # New semantic sections present
    assert "trend_4h" in body
    assert "structure_1h" in body
    assert "microstructure_15m" in body
    # Old raw-candle keys gone
    assert "candles_4h_last_10" not in body
    assert "candles_1h_last_12" not in body
    assert "candles_15m_last_16" not in body


def test_user_content_token_budget_under_2500_chars():
    """Phase 8b token-budget claim: semantic context must be substantially
    smaller than the old raw-candle dump. Old format ran ~3000+ chars on
    realistic data; new format should fit well under 2500 chars even with
    small verification snapshots."""
    client = _FakeClient({
        "action": "HOLD", "confidence": 5, "rationale": "x",
    })
    _call(client)
    body = client.last_user_content
    assert len(body) < 2500, (
        f"user_content grew to {len(body)} chars — semantic context bloating?"
    )
