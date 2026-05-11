"""Claude CLI backend — uses the user's Claude.ai plan quota (OAuth) instead of API credits.

How it works:
- Shells out to the `claude` CLI binary in non-interactive (`-p`) mode
- System prompt via `--append-system-prompt`, user content on stdin
- Returns parsed JSON (CLI returns text; we strip code fences and parse)

When to use:
- User has Claude Pro/Max plan and prefers to spend plan quota
- ANTHROPIC_API_KEY not set / no API credit balance
- claude CLI installed + logged in via `claude /login`

Trade-offs vs API:
- Slower: ~4-8 seconds per call (CLI cold start) vs ~1-3 seconds API
- Plan quota usage instead of API billing
- No prompt caching exposed (claude CLI manages internally)
- Same coherence + intelligence as API
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Final

from agents.client import AgentClientError

DEFAULT_CLAUDE_BIN: Final = "claude"
# Cold start can be slow under cron (OAuth refresh etc.); be generous.
DEFAULT_TIMEOUT_SEC: Final = 90
DEFAULT_MODEL_ALIAS: Final = "sonnet"


@dataclass
class ClaudeCliClient:
    """Plan-quota-backed client via the `claude` CLI in print mode."""

    claude_bin: str = DEFAULT_CLAUDE_BIN
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    model: str = DEFAULT_MODEL_ALIAS

    def call_structured(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 1024,  # claude CLI manages this itself
        temperature: float = 0.0,  # not exposed by CLI; relying on default
    ) -> dict[str, Any]:
        cmd = [
            self.claude_bin,
            "-p",
            "--output-format", "text",
            "--append-system-prompt", system_prompt,
            "--model", self.model,
            "--no-session-persistence",
        ]
        try:
            result = subprocess.run(
                cmd,
                input=user_content,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as e:
            raise AgentClientError(
                f"claude CLI timeout after {self.timeout_sec}s"
            ) from e
        except FileNotFoundError as e:
            raise AgentClientError(
                f"claude binary not found at {self.claude_bin!r}"
            ) from e

        if result.returncode != 0:
            tail = (result.stderr or result.stdout)[-300:].strip()
            raise AgentClientError(
                f"claude CLI exit {result.returncode}: {tail}"
            )

        body = result.stdout.strip()
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
            raise AgentClientError(
                f"response was not valid JSON: {body[:300]}"
            ) from e


def cli_available(claude_bin: str = DEFAULT_CLAUDE_BIN) -> bool:
    """Whether the claude binary is on PATH (or at given location)."""
    return shutil.which(claude_bin) is not None
