"""Tests for the vision-augmented position advisor (Phase 8c PR-2).

Mocks both the text AgentClient and the multimodal VisionClient. The
chart renderer is also mocked so these tests stay fast — chart_renderer
itself is covered in tests/test_chart_renderer.py.
"""
from __future__ import annotations

import pandas as pd
import pytest

from agents import position_advisor
from agents.position_advisor import (
    AdvisorOutput,
    VisionVerdict,
    _merge_actions,
    evaluate,
)


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


class _FakeTextClient:
    """Stub AgentClient that returns one canned text response."""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.last_user_content: str | None = None

    def call_structured(self, *, system_prompt, user_content, **_kw):
        self.last_user_content = user_content
        return self.response


class _FakeVisionClient:
    """Stub VisionClient capturing multimodal payloads for assertions."""

    def __init__(self, response: dict | Exception) -> None:
        self.response = response
        self.last_call: dict | None = None
        self.call_count: int = 0

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
    """Always return a tiny fake PNG b64 — fast and deterministic."""
    return mocker.patch.object(
        position_advisor, "render_composite_chart", return_value="iVBORw0KGgoFAKE",
    )


def _call(
    *,
    text_response,
    vision_response=None,
    vision_client_arg="auto",
    sl: float | None = 79000.0,
    tp: float | None = 82000.0,
) -> tuple[AdvisorOutput, _FakeTextClient, _FakeVisionClient | None]:
    text = _FakeTextClient(text_response)
    if vision_client_arg == "auto":
        vision = (
            _FakeVisionClient(vision_response) if vision_response is not None else None
        )
    else:
        vision = vision_client_arg  # explicit None pass-through
    out = evaluate(
        symbol="BTCUSDT", direction="long",
        entry=80000.0, current_price=81000.0, pnl_pct=1.25,
        df_4h=_klines(), df_1h=_klines(), df_15m=_klines(),
        client=text,
        stop_loss=sl, take_profit=tp,
        vision_client=vision,
    )
    return out, text, vision


# --- merge rule -----------------------------------------------------------


@pytest.mark.parametrize(
    "text_a, vision_a, expected_action, expected_overrode",
    [
        ("HOLD", "HOLD", "HOLD", False),
        ("HOLD", "TIGHTEN", "TIGHTEN", True),
        ("HOLD", "EXIT", "EXIT", True),
        ("PARTIAL", "EXIT", "EXIT", True),
        # downgrade-only: vision-less-conservative is ignored
        ("EXIT", "HOLD", "EXIT", False),
        ("PARTIAL", "HOLD", "PARTIAL", False),
        ("TIGHTEN", "TIGHTEN", "TIGHTEN", False),
    ],
)
def test_merge_actions_only_escalates(
    text_a, vision_a, expected_action, expected_overrode,
):
    final, overrode = _merge_actions(text_a, vision_a)
    assert final == expected_action
    assert overrode is expected_overrode


# --- vision off / not provided -------------------------------------------


def test_no_vision_client_falls_back_to_text_only(monkeypatch, stub_chart):
    """When no vision_client is passed, behavior matches the pre-PR-2 advisor."""
    monkeypatch.delenv("VISION_DISABLED", raising=False)
    monkeypatch.delenv("VISION_ADVISOR_DISABLED", raising=False)
    out, _, _ = _call(
        text_response={"action": "HOLD", "confidence": 7, "rationale": "intact"},
        vision_client_arg=None,
    )
    assert out.action == "HOLD"
    assert out.vision is None
    assert out.vision_overrode is False
    assert stub_chart.call_count == 0  # chart never rendered


def test_advisor_disabled_env_skips_vision_even_with_client(monkeypatch, stub_chart):
    monkeypatch.setenv("VISION_ADVISOR_DISABLED", "1")
    out, _, vision = _call(
        text_response={"action": "HOLD", "confidence": 7, "rationale": "intact"},
        vision_response={"action": "EXIT", "confidence": 9, "rationale": "broke"},
    )
    assert out.action == "HOLD"  # vision blocked by env toggle
    assert out.vision is None
    assert vision.call_count == 0
    assert stub_chart.call_count == 0


def test_global_vision_disable_skips_vision(monkeypatch, stub_chart):
    monkeypatch.setenv("VISION_DISABLED", "1")
    out, _, vision = _call(
        text_response={"action": "HOLD", "confidence": 7, "rationale": "intact"},
        vision_response={"action": "EXIT", "confidence": 9, "rationale": "broke"},
    )
    assert out.action == "HOLD"
    assert out.vision is None
    assert vision.call_count == 0


# --- vision on, enforced --------------------------------------------------


def test_vision_overrides_to_more_conservative(monkeypatch, stub_chart):
    monkeypatch.delenv("VISION_DISABLED", raising=False)
    monkeypatch.delenv("VISION_ADVISOR_DISABLED", raising=False)
    monkeypatch.delenv("VISION_OVERRIDE_DRY_RUN", raising=False)
    out, _, vision = _call(
        text_response={"action": "HOLD", "confidence": 6, "rationale": "weak hold"},
        vision_response={
            "action": "EXIT",
            "confidence": 9,
            "rationale": "swing low broken on 1H with volume",
        },
    )
    assert out.action == "EXIT"
    assert out.vision is not None and out.vision.action == "EXIT"
    assert out.vision_overrode is True
    assert out.dry_run is False
    assert vision.call_count == 1
    assert stub_chart.call_count == 1
    # rationale should mention the override so the alert text carries it.
    assert "vision override" in out.rationale


def test_vision_agreement_keeps_text_action(monkeypatch, stub_chart):
    out, _, vision = _call(
        text_response={"action": "TIGHTEN", "confidence": 7, "rationale": "cooling"},
        vision_response={"action": "TIGHTEN", "confidence": 8, "rationale": "agree"},
    )
    assert out.action == "TIGHTEN"
    assert out.vision_overrode is False
    assert out.vision.action == "TIGHTEN"


def test_vision_cannot_relax_action(monkeypatch, stub_chart):
    """Vision suggests HOLD but text says EXIT — text must stand."""
    out, _, _ = _call(
        text_response={"action": "EXIT", "confidence": 9, "rationale": "broken"},
        vision_response={"action": "HOLD", "confidence": 4, "rationale": "still ok"},
    )
    assert out.action == "EXIT"
    assert out.vision_overrode is False


# --- dry-run mode --------------------------------------------------------


def test_dry_run_records_but_does_not_enforce(monkeypatch, stub_chart):
    monkeypatch.setenv("VISION_OVERRIDE_DRY_RUN", "1")
    out, _, vision = _call(
        text_response={"action": "HOLD", "confidence": 6, "rationale": "weak hold"},
        vision_response={"action": "EXIT", "confidence": 9, "rationale": "broke"},
    )
    assert out.action == "HOLD"          # NOT EXIT — dry run
    assert out.dry_run is True
    assert out.vision is not None and out.vision.action == "EXIT"
    assert out.vision_overrode is False  # dry run never enforces
    assert vision.call_count == 1        # call still happened (for telemetry)


# --- vision failure modes ------------------------------------------------


def test_vision_api_failure_does_not_break_text_path(monkeypatch, stub_chart):
    """A vision-client exception must surface as a failed VisionVerdict,
    NOT take down the text-only advisor recommendation."""
    from agents.sdk_client import VisionClientError

    out, _, _ = _call(
        text_response={"action": "TIGHTEN", "confidence": 7, "rationale": "cooling"},
        vision_response=VisionClientError("daily cost cap reached"),
    )
    assert out.action == "TIGHTEN"          # text decision stands
    assert out.vision is not None
    assert out.vision.failed is True
    assert "cost cap" in out.vision.failure_reason
    assert out.vision_overrode is False


def test_vision_invalid_json_action_treated_as_failure(monkeypatch, stub_chart):
    out, _, _ = _call(
        text_response={"action": "HOLD", "confidence": 7, "rationale": "ok"},
        vision_response={"action": "BUY", "confidence": 5, "rationale": "?"},
    )
    assert out.action == "HOLD"
    assert out.vision.failed is True
    assert "invalid action" in out.vision.failure_reason


def test_chart_render_failure_falls_back_to_text(monkeypatch, mocker):
    """If matplotlib chokes on the data, vision is skipped, not a crash."""
    mocker.patch.object(
        position_advisor, "render_composite_chart", side_effect=RuntimeError("oom"),
    )
    out, _, vision = _call(
        text_response={"action": "HOLD", "confidence": 7, "rationale": "ok"},
        vision_response={"action": "EXIT", "confidence": 9, "rationale": "?"},
    )
    assert out.action == "HOLD"
    assert out.vision.failed is True
    assert "chart render" in out.vision.failure_reason
    assert vision.call_count == 0


# --- multimodal payload shape --------------------------------------------


def test_multimodal_payload_includes_chart_and_text_advisor_context(
    monkeypatch, stub_chart,
):
    out, _, vision = _call(
        text_response={
            "action": "HOLD",
            "confidence": 6,
            "rationale": "weak but intact",
        },
        vision_response={"action": "HOLD", "confidence": 7, "rationale": "agree"},
    )
    assert vision.last_call is not None
    # Exactly one image attached.
    assert len(vision.last_call["images"]) == 1
    img = vision.last_call["images"][0]
    assert img.media_type == "image/png"
    assert img.data == "iVBORw0KGgoFAKE"
    # Vision sees the text advisor's tentative verdict (so it can choose to
    # confirm or escalate consciously).
    text_block = vision.last_call["text_content"]
    assert "action: HOLD" in text_block
    assert "weak but intact" in text_block
