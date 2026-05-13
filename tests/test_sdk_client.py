"""Unit tests for agents.sdk_client (Phase 8c PR-1).

All Anthropic calls are mocked — no live API hits.
"""
from __future__ import annotations

import pytest

from agents.client import AgentClientError
from agents.sdk_client import (
    ANTHROPIC_API_KEY_ENV,
    ImagePart,
    VisionClient,
    VisionClientError,
    get_vision_client_or_none,
)


# A tiny base64 string — content is irrelevant since the SDK is mocked.
_FAKE_PNG_B64 = "iVBORw0KGgoAAAANS"


class _FakeText:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)


class _FakeMessage:
    def __init__(self, body: str, usage: _FakeUsage | None = None) -> None:
        self.content = [_FakeText(body)]
        self.usage = usage


def _stub_create(client: VisionClient, mocker, body: str, *, usage=None):
    """Replace the SDK transport with a stub that returns the given body."""
    captured: dict = {}

    def _create(**kw):
        captured.update(kw)
        return _FakeMessage(body, usage=usage)

    mocker.patch.object(
        client, "_client", mocker.Mock(messages=mocker.Mock(create=_create))
    )
    return captured


# --- construction --------------------------------------------------------


def test_construction_requires_api_key(monkeypatch):
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    with pytest.raises(VisionClientError, match="ANTHROPIC_API_KEY"):
        VisionClient()


def test_vision_client_error_is_agent_client_error_subclass():
    # Existing graceful-degradation handlers catch AgentClientError —
    # VisionClientError must remain a subclass for that to keep working.
    assert issubclass(VisionClientError, AgentClientError)


def test_get_vision_client_or_none_respects_kill_switch(monkeypatch):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    monkeypatch.setenv("VISION_DISABLED", "1")
    assert get_vision_client_or_none() is None


def test_get_vision_client_or_none_returns_client_when_enabled(monkeypatch):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    monkeypatch.delenv("VISION_DISABLED", raising=False)
    assert get_vision_client_or_none() is not None


# --- call_multimodal -----------------------------------------------------


def test_multimodal_payload_places_image_first_and_caches(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = VisionClient()
    captured = _stub_create(c, mocker, '{"verdict": "ENTER"}')

    out = c.call_multimodal(
        system_prompt="sys",
        text_content="please analyze",
        images=[ImagePart(data=_FAKE_PNG_B64, media_type="image/png")],
    )
    assert out == {"verdict": "ENTER"}
    blocks = captured["messages"][0]["content"]
    # Image must precede text — model attends to it more reliably this way.
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[-1]["type"] == "text"
    # System is cached too.
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_multimodal_requires_at_least_one_image(monkeypatch):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = VisionClient()
    with pytest.raises(VisionClientError, match="at least one image"):
        c.call_multimodal(system_prompt="s", text_content="t", images=[])


def test_image_part_rejects_unknown_media_type(monkeypatch):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = VisionClient()
    with pytest.raises(VisionClientError, match="unsupported image media type"):
        c.call_multimodal(
            system_prompt="s",
            text_content="t",
            images=[ImagePart(data=_FAKE_PNG_B64, media_type="image/bmp")],
        )


# --- call_structured + JSON parsing --------------------------------------


def test_structured_strips_markdown_fence(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = VisionClient()
    _stub_create(c, mocker, '```json\n{"x": 1}\n```')
    assert c.call_structured(system_prompt="s", user_content="u") == {"x": 1}


def test_structured_raises_on_invalid_json(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    c = VisionClient()
    _stub_create(c, mocker, "not json")
    with pytest.raises(VisionClientError, match="not valid JSON"):
        c.call_structured(system_prompt="s", user_content="u")


# --- cost_guard integration ---------------------------------------------


def test_cost_guard_blocks_call_when_disallowed(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")

    class _BlockingGuard:
        def allow(self) -> bool:
            return False

        def record(self, *_a, **_kw) -> None:  # pragma: no cover
            raise AssertionError("record() must not run when allow() is False")

    c = VisionClient(cost_guard=_BlockingGuard())
    _stub_create(c, mocker, '{"x": 1}')
    with pytest.raises(VisionClientError, match="cost cap"):
        c.call_structured(system_prompt="s", user_content="u")


def test_cost_guard_records_usage_after_successful_call(monkeypatch, mocker):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")
    recorded: list = []

    class _RecordingGuard:
        def allow(self) -> bool:
            return True

        def record(self, usage, *, model) -> None:
            recorded.append((usage, model))

    c = VisionClient(cost_guard=_RecordingGuard(), model="claude-sonnet-4-6")
    _stub_create(
        c, mocker, '{"ok": true}',
        usage=_FakeUsage(input_tokens=100, output_tokens=50),
    )
    c.call_structured(system_prompt="s", user_content="u")
    assert len(recorded) == 1
    usage, model = recorded[0]
    assert usage.input_tokens == 100
    assert model == "claude-sonnet-4-6"


def test_cost_guard_record_failure_does_not_break_call(monkeypatch, mocker):
    """If cost tracking blows up, the API result must still come back."""
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-fake")

    class _BrokenGuard:
        def allow(self) -> bool:
            return True

        def record(self, *_a, **_kw) -> None:
            raise RuntimeError("disk full")

    c = VisionClient(cost_guard=_BrokenGuard())
    _stub_create(c, mocker, '{"ok": true}', usage=_FakeUsage(input_tokens=1, output_tokens=1))
    assert c.call_structured(system_prompt="s", user_content="u") == {"ok": True}
