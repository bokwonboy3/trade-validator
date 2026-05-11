"""Anthropic API client wrapper.

Centralized so all specialists share:
- Same model + temperature defaults
- Prompt caching for static system prompts (~70% input savings)
- Structured JSON output enforcement
- Timeout + retry policy
- Graceful failure (returns None instead of raising)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Final

import anthropic

ANTHROPIC_API_KEY_ENV: Final = "ANTHROPIC_API_KEY"
DEFAULT_MODEL: Final = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS: Final = 1024
DEFAULT_TIMEOUT_SEC: Final = 30


class AgentClientError(RuntimeError):
    """Raised when the Anthropic API call fails after exhausting retries."""


@dataclass
class AgentClient:
    """Thin wrapper around anthropic.Anthropic with project defaults."""

    api_key: str | None = None
    model: str = DEFAULT_MODEL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
        if not key:
            raise AgentClientError(
                f"{ANTHROPIC_API_KEY_ENV} env var not set — agentic analysis disabled. "
                f"Set it in .tv-env to enable."
            )
        self._client = anthropic.Anthropic(api_key=key, timeout=self.timeout_sec)

    def call_structured(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Single call. System prompt is cached (5min TTL). Response parsed as JSON.

        Raises AgentClientError on:
          - API error after retry
          - Response not valid JSON
          - Timeout
        """
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.APIError as e:
            raise AgentClientError(f"Anthropic API error: {e}") from e

        if not msg.content or msg.content[0].type != "text":
            raise AgentClientError(f"unexpected response shape: {msg!r}")
        body = msg.content[0].text.strip()
        # Models sometimes wrap JSON in ```json fences — strip them.
        if body.startswith("```"):
            lines = body.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            body = "\n".join(lines).strip()
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise AgentClientError(f"response was not valid JSON: {body[:300]}") from e


def get_default_client_or_none() -> AgentClient | None:
    """Try to construct a client; return None if API key missing.

    Callers use this to gracefully disable agentic features when the user
    hasn't set up an API key yet.
    """
    try:
        return AgentClient()
    except AgentClientError:
        return None
