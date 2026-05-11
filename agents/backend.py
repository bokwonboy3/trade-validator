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


def get_default_client() -> Any | None:
    """Return a client (CLI or API) per selection rules above, or None."""
    backend = os.environ.get(AGENT_BACKEND_ENV, "").strip().lower()

    if backend == "none":
        return None

    if backend == "cli":
        return ClaudeCliClient() if cli_available() else None

    if backend == "api":
        try:
            return AgentClient()
        except AgentClientError:
            return None

    # default: try CLI first (user's plan), then API
    if cli_available():
        return ClaudeCliClient()
    try:
        return AgentClient()
    except AgentClientError:
        return None
