"""Tests for the pre-entry vision check (Phase 8c PR-3)."""
from __future__ import annotations

import pandas as pd
import pytest

from agents import vision_check
from agents.types import AgentVerdict
from agents.vision_check import _merge, apply_vision_check


def _klines(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "openTime": list(range(n)),
            "open": [80000] * n,
            "high": [80100] * n,
            "low": [79900] * n,
            "close": [80050] * n,
            "volume": [1.0] * n,
            "closeTime": list(range(1, n + 1)),
        }
    )


def _verdict(v: str = "ENTER", *, conf: int = 80, rationale: str = "ok") -> AgentVerdict:
    return AgentVerdict(
        verdict=v, confidence=conf, rationale=rationale,
        tier1_verdict=v, downgraded_from_tier1=False,
    )


class _FakeVisionClient:
    def __init__(self, response) -> None:
        self.response = response
        self.call_count = 0
        self.last_call: dict | None = None

    def call_multimodal(self, *, system_prompt, text_content, images, **_kw):
        self.call_count += 1
        self.last_call = {
            "system_prompt": system_prompt,
            "text_content": text_content,
            "images": list(images),
        }
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def stub_chart(mocker):
    """Skip the (slow) real chart render. chart_renderer has its own tests."""
    return mocker.patch.object(
        vision_check, "render_composite_chart", return_value="iVBORw0KGgoFAKE",
    )


def _call(
    *,
    base_verdict: str = "ENTER",
    vision_response=None,
    vision_client="auto",
):
    if vision_client == "auto":
        client = (
            _FakeVisionClient(vision_response) if vision_response is not None else None
        )
    else:
        client = vision_client
    out = apply_vision_check(
        _verdict(base_verdict),
        df_4h=_klines(), df_1h=_klines(), df_15m=_klines(),
        symbol="BTCUSDT", direction="long",
        entry=80000.0, sl=79000.0, tp=83000.0,
        vision_client=client,
    )
    return out, client


# --- merge rule -----------------------------------------------------------


@pytest.mark.parametrize(
    "text_v, vision_v, expected, expected_overrode",
    [
        # Vision can downgrade
        ("ENTER",   "PLAN_OK", "PLAN_OK", True),
        ("ENTER",   "WATCH",   "WATCH",   True),
        ("ENTER",   "PASS",    "PASS",    True),
        ("PLAN_OK", "WATCH",   "WATCH",   True),
        # Same verdict → no override
        ("ENTER",   "ENTER",   "ENTER",   False),
        ("WATCH",   "WATCH",   "WATCH",   False),
        # Vision-MORE-bullish must be IGNORED (upgrade blocked)
        ("WATCH",   "ENTER",   "WATCH",   False),
        ("PASS",    "ENTER",   "PASS",    False),
        ("PLAN_OK", "ENTER",   "PLAN_OK", False),
    ],
)
def test_merge_downgrade_only(text_v, vision_v, expected, expected_overrode):
    final, overrode = _merge(text_v, vision_v)
    assert final == expected
    assert overrode is expected_overrode


# --- vision off / not provided -------------------------------------------


def test_no_vision_client_is_passthrough(monkeypatch, stub_chart):
    monkeypatch.delenv("VISION_DISABLED", raising=False)
    monkeypatch.delenv("VISION_CHECK_DISABLED", raising=False)
    out, _ = _call(base_verdict="ENTER", vision_client=None)
    assert out.verdict == "ENTER"
    assert out.vision is None
    assert stub_chart.call_count == 0


def test_check_disabled_env_skips_vision(monkeypatch, stub_chart):
    monkeypatch.setenv("VISION_CHECK_DISABLED", "1")
    out, client = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "PASS", "confidence": 9, "rationale": "no"},
    )
    assert out.verdict == "ENTER"
    assert out.vision is None
    assert client.call_count == 0
    assert stub_chart.call_count == 0


def test_global_vision_disable_skips_check(monkeypatch, stub_chart):
    monkeypatch.setenv("VISION_DISABLED", "1")
    out, client = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "PASS", "confidence": 9, "rationale": "no"},
    )
    assert out.verdict == "ENTER"
    assert client.call_count == 0


# --- vision on, enforced --------------------------------------------------


def test_vision_downgrades_enter_to_watch(monkeypatch, stub_chart):
    monkeypatch.delenv("VISION_DISABLED", raising=False)
    monkeypatch.delenv("VISION_CHECK_DISABLED", raising=False)
    monkeypatch.delenv("VISION_OVERRIDE_DRY_RUN", raising=False)
    out, client = _call(
        base_verdict="ENTER",
        vision_response={
            "verdict": "WATCH",
            "confidence": 8,
            "rationale": "bearish divergence on 1H RSI vs price",
        },
    )
    assert out.verdict == "WATCH"
    assert out.vision is not None and out.vision.verdict == "WATCH"
    assert out.vision_overrode is True
    assert out.vision_dry_run is False
    assert client.call_count == 1
    assert stub_chart.call_count == 1
    assert "vision override" in out.rationale


def test_vision_agreement_keeps_verdict(monkeypatch, stub_chart):
    out, _ = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "ENTER", "confidence": 8, "rationale": "agree"},
    )
    assert out.verdict == "ENTER"
    assert out.vision_overrode is False
    assert out.vision.verdict == "ENTER"


def test_vision_cannot_upgrade(monkeypatch, stub_chart):
    """Vision suggests ENTER but text says WATCH → text stands."""
    out, _ = _call(
        base_verdict="WATCH",
        vision_response={"verdict": "ENTER", "confidence": 8, "rationale": "looks good"},
    )
    assert out.verdict == "WATCH"
    assert out.vision_overrode is False


# --- dry-run mode --------------------------------------------------------


def test_dry_run_records_but_does_not_enforce(monkeypatch, stub_chart):
    monkeypatch.setenv("VISION_OVERRIDE_DRY_RUN", "1")
    out, client = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "PASS", "confidence": 9, "rationale": "broken"},
    )
    assert out.verdict == "ENTER"           # NOT downgraded
    assert out.vision_dry_run is True
    assert out.vision is not None and out.vision.verdict == "PASS"
    assert out.vision_overrode is False
    assert client.call_count == 1           # call still happened (telemetry)


# --- failure modes -------------------------------------------------------


def test_vision_api_failure_does_not_break_pipeline(monkeypatch, stub_chart):
    from agents.sdk_client import VisionClientError

    out, _ = _call(
        base_verdict="ENTER",
        vision_response=VisionClientError("daily cost cap reached"),
    )
    assert out.verdict == "ENTER"           # enforced verdict stands
    assert out.vision is not None
    assert out.vision.failed is True
    assert "cost cap" in out.vision.failure_reason
    assert out.vision_overrode is False


def test_invalid_vision_verdict_treated_as_failure(monkeypatch, stub_chart):
    out, _ = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "MAYBE", "confidence": 5, "rationale": "?"},
    )
    assert out.verdict == "ENTER"
    assert out.vision.failed is True
    assert "invalid verdict" in out.vision.failure_reason


def test_invalid_confidence_treated_as_failure(monkeypatch, stub_chart):
    out, _ = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "WATCH", "confidence": "not-a-number", "rationale": "?"},
    )
    assert out.verdict == "ENTER"
    assert out.vision.failed is True


def test_chart_render_failure_falls_back(monkeypatch, mocker):
    mocker.patch.object(
        vision_check, "render_composite_chart", side_effect=RuntimeError("oom"),
    )
    out, client = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "PASS", "confidence": 9, "rationale": "?"},
    )
    assert out.verdict == "ENTER"
    assert out.vision.failed is True
    assert "chart render" in out.vision.failure_reason
    assert client.call_count == 0


# --- multimodal payload shape --------------------------------------------


def test_multimodal_payload_includes_chart_levels_and_text_context(
    monkeypatch, stub_chart,
):
    out, client = _call(
        base_verdict="ENTER",
        vision_response={"verdict": "ENTER", "confidence": 8, "rationale": "agree"},
    )
    assert client.last_call is not None
    # Exactly one PNG image attached.
    imgs = client.last_call["images"]
    assert len(imgs) == 1
    assert imgs[0].media_type == "image/png"
    assert imgs[0].data == "iVBORw0KGgoFAKE"
    # Text block includes the trade candidate identifiers + text tentative verdict.
    text_block = client.last_call["text_content"]
    assert "symbol: BTCUSDT" in text_block
    assert "direction: long" in text_block
    assert "entry: 80000" in text_block
    assert "stop_loss: 79000" in text_block
    assert "take_profit: 83000" in text_block
    assert "verdict: ENTER" in text_block
