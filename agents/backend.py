"""Backend selection: pick claude CLI (plan quota) or Anthropic API (credits).

Selection order:
  1. AGENT_BACKEND env var ("cli" | "api" | "none"):
       cli  → use claude CLI (plan quota), None if claude binary missing
       api  → use anthropic SDK (API credits), None if ANTHROPIC_API_KEY missing
       none → disable agentic tier entirely
  2. Default (no env var): try CLI first (plan quota), fall back to API.

Both backends expose the same `call_structured(system_prompt, user_content)`
interface, so the runner doesn't care which one is in use.
"""
from __future__ import annotations

import os
from typing import Any, Final

from agents.cli_client import ClaudeCliClient, cli_available
from agents.client import AgentClient, AgentClientError

AGENT_BACKEND_ENV: Final = "AGENT_BACKEND"
AGENT_MODEL_ENV: Final = "AGENT_MODEL"


def _model_kwarg(override: str | None = None) -> dict[str, str]:
    """Pass `model=` only when explicitly set, otherwise use the client's own
    default. Precedence: ``override`` argument > ``AGENT_MODEL`` env var.
    Same alias works for both CLI and API backends — CLI accepts aliases
    (`sonnet`, `opus`, `haiku`) and full names; API expects full names
    (`claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5-20251001`)."""
    if override:
        return {"model": override.strip()}
    m = os.environ.get(AGENT_MODEL_ENV, "").strip()
    return {"model": m} if m else {}


def get_default_client(model: str | None = None) -> Any | None:
    """Return a client (CLI or API) per selection rules above, or None.

    Args:
        model: explicit model override. Takes precedence over the AGENT_MODEL
            env var. Use this for per-call-site model tiering (e.g., Haiku for
            the position monitor's lightweight checks vs. Sonnet for entry
            decisions). If None, falls back to AGENT_MODEL, then client default.
    """
    backend = os.environ.get(AGENT_BACKEND_ENV, "").strip().lower()
    model_kw = _model_kwarg(override=model)

    if backend == "none":
        return None

    if backend == "cli":
        return ClaudeCliClient(**model_kw) if cli_available() else None

    if backend == "api":
        try:
            return AgentClient(**model_kw)
        except AgentClientError:
            return None

    # default: try CLI first (user's plan), then API
    if cli_available():
        return ClaudeCliClient(**model_kw)
    try:
        return AgentClient(**model_kw)
    except AgentClientError:
        return None
