"""Tests for the claude CLI backend + backend picker.

Live CLI invocation is marked @live (skipped by default). Pure subprocess
mocking covers the success/failure paths.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agents.backend import AGENT_BACKEND_ENV, get_default_client
from agents.cli_client import (
    DEFAULT_CLAUDE_BIN,
    ClaudeCliClient,
    cli_available,
)
from agents.client import AgentClient, AgentClientError


# --- cli_available ---
def test_cli_available_with_existing_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude")
    assert cli_available()


def test_cli_available_with_missing_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert not cli_available()


# --- ClaudeCliClient.call_structured ---
def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a fake CompletedProcess for subprocess.run."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


def test_cli_call_parses_plain_json(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=_fake_run(stdout='{"verdict": "ENTER", "confidence": 8}'),
    )
    c = ClaudeCliClient()
    out = c.call_structured(system_prompt="sys", user_content="data")
    assert out == {"verdict": "ENTER", "confidence": 8}


def test_cli_call_strips_markdown_fence(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=_fake_run(stdout='```json\n{"v": "WATCH"}\n```'),
    )
    c = ClaudeCliClient()
    out = c.call_structured(system_prompt="sys", user_content="data")
    assert out == {"v": "WATCH"}


def test_cli_call_raises_on_nonzero_exit(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=_fake_run(stdout="", stderr="auth required", returncode=1),
    )
    c = ClaudeCliClient()
    with pytest.raises(AgentClientError, match="exit 1"):
        c.call_structured(system_prompt="sys", user_content="data")


def test_cli_call_raises_on_timeout(mocker):
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
    )
    c = ClaudeCliClient(timeout_sec=30)
    with pytest.raises(AgentClientError, match="timeout"):
        c.call_structured(system_prompt="sys", user_content="data")


def test_cli_call_raises_when_binary_missing(mocker):
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("no such file"))
    c = ClaudeCliClient(claude_bin="/nonexistent/claude")
    with pytest.raises(AgentClientError, match="not found"):
        c.call_structured(system_prompt="sys", user_content="data")


def test_cli_call_raises_on_invalid_json(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=_fake_run(stdout="not json at all"),
    )
    c = ClaudeCliClient()
    with pytest.raises(AgentClientError, match="not valid JSON"):
        c.call_structured(system_prompt="sys", user_content="data")


def test_cli_call_passes_system_prompt_via_append_flag(mocker):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _fake_run(stdout='{"v": "WATCH"}')

    mocker.patch("subprocess.run", side_effect=fake_run)
    c = ClaudeCliClient()
    c.call_structured(system_prompt="SYS_PROMPT", user_content="USER_DATA")

    assert "--append-system-prompt" in captured["cmd"]
    idx = captured["cmd"].index("--append-system-prompt")
    assert captured["cmd"][idx + 1] == "SYS_PROMPT"
    assert captured["input"] == "USER_DATA"


# --- Backend picker ---
def test_backend_none_disables(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "none")
    assert get_default_client() is None


def test_backend_cli_returns_cli_when_available(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "cli")
    monkeypatch.setattr("agents.backend.cli_available", lambda: True)
    c = get_default_client()
    assert isinstance(c, ClaudeCliClient)


def test_backend_cli_returns_none_when_missing(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "cli")
    monkeypatch.setattr("agents.backend.cli_available", lambda: False)
    assert get_default_client() is None


def test_backend_api_uses_api_client(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    c = get_default_client()
    assert isinstance(c, AgentClient)


def test_backend_api_returns_none_without_key(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert get_default_client() is None


def test_backend_default_prefers_cli(monkeypatch):
    """No env var → CLI takes precedence over API."""
    monkeypatch.delenv(AGENT_BACKEND_ENV, raising=False)
    monkeypatch.setattr("agents.backend.cli_available", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    c = get_default_client()
    assert isinstance(c, ClaudeCliClient)


def test_backend_default_falls_back_to_api(monkeypatch):
    monkeypatch.delenv(AGENT_BACKEND_ENV, raising=False)
    monkeypatch.setattr("agents.backend.cli_available", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    c = get_default_client()
    assert isinstance(c, AgentClient)


def test_backend_default_returns_none_when_both_missing(monkeypatch):
    monkeypatch.delenv(AGENT_BACKEND_ENV, raising=False)
    monkeypatch.setattr("agents.backend.cli_available", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert get_default_client() is None


# --- AGENT_MODEL override ---
def test_agent_model_override_cli(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "cli")
    monkeypatch.setenv("AGENT_MODEL", "opus")
    monkeypatch.setattr("agents.backend.cli_available", lambda: True)
    c = get_default_client()
    assert isinstance(c, ClaudeCliClient)
    assert c.model == "opus"


def test_agent_model_override_api(monkeypatch):
    monkeypatch.setenv(AGENT_BACKEND_ENV, "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("AGENT_MODEL", "claude-opus-4-7")
    c = get_default_client()
    assert isinstance(c, AgentClient)
    assert c.model == "claude-opus-4-7"


def test_agent_model_default_when_not_set(monkeypatch):
    """No AGENT_MODEL → client uses its own default (Sonnet)."""
    monkeypatch.setenv(AGENT_BACKEND_ENV, "cli")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setattr("agents.backend.cli_available", lambda: True)
    c = get_default_client()
    assert c.model == "sonnet"  # ClaudeCliClient DEFAULT_MODEL_ALIAS


# --- Live CLI test (skipped unless --run-live) ---
@pytest.mark.live
def test_live_cli_returns_json():
    """Actually invoke claude CLI — needs `claude` on PATH AND OAuth login."""
    if not cli_available():
        pytest.skip("claude binary not on PATH")
    c = ClaudeCliClient(timeout_sec=60)
    out = c.call_structured(
        system_prompt="Reply with strict JSON only. No prose, no markdown fences.",
        user_content='Echo this back exactly: {"echo": "hello", "n": 42}',
    )
    assert isinstance(out, dict)
    # Best-effort — the model might rephrase. Just check structure.
    assert len(out) > 0
