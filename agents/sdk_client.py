"""Multimodal Anthropic SDK client for vision-tier agents (Phase 8c).

Why a second client (vs. extending ``agents.client.AgentClient``)?
- Vision payloads cost ~10× a text-only specialist call, so they need a
  hard daily cap (``agents.cost_guard``) wired into every call.
- The vision tier is opt-in via env var and only ever runs against the
  API backend (the claude CLI does not accept image input on the
  Messages API surface we use elsewhere).

Surface:
    VisionClient(api_key=None, model=..., timeout_sec=60, cost_guard=...)
        .call_structured(system_prompt, user_content, ...)  -> dict   # text-only, JSON
        .call_multimodal(system_prompt, text_content, images, ...) -> dict

``call_structured`` matches ``AgentClient.call_structured`` so existing
specialist code can be flipped to the vision client without other changes
(useful when the vision-disabled config still wants the SDK path).

The system prompt is always cached (5-min ephemeral cache). Each image is
also marked ``cache_control: ephemeral`` so re-asking the same chart in a
follow-up call gets the cached read price.

Failures: every error path raises ``VisionClientError`` (a subclass of
``AgentClientError`` so existing graceful-degradation handlers catch both).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Final, Sequence

import anthropic

from agents.client import AgentClientError

ANTHROPIC_API_KEY_ENV: Final = "ANTHROPIC_API_KEY"
DEFAULT_VISION_MODEL: Final = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS: Final = 2048
DEFAULT_TIMEOUT_SEC: Final = 60  # vision is slower than text-only
SUPPORTED_IMAGE_TYPES: Final = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


class VisionClientError(AgentClientError):
    """Raised when a multimodal Anthropic call fails or returns bad shape."""


@dataclass
class ImagePart:
    """One image attachment for ``call_multimodal``.

    ``data`` must be base64-encoded (no data: URI prefix). ``media_type``
    must be one of ``SUPPORTED_IMAGE_TYPES``.
    """

    data: str
    media_type: str = "image/png"

    def to_block(self, *, cache: bool = True) -> dict[str, Any]:
        if self.media_type not in SUPPORTED_IMAGE_TYPES:
            raise VisionClientError(
                f"unsupported image media type: {self.media_type!r}"
            )
        block: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.data,
            },
        }
        if cache:
            block["cache_control"] = {"type": "ephemeral"}
        return block


@dataclass
class VisionClient:
    """Anthropic SDK wrapper for vision-capable models.

    ``cost_guard`` is any object with ``allow(estimated_cost_usd: float) ->
    bool`` and ``record(usage_block, model: str) -> None``. Pass ``None``
    to disable cost gating (still cheap to do in tests).
    """

    api_key: str | None = None
    model: str = DEFAULT_VISION_MODEL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    cost_guard: Any | None = None
    _client: anthropic.Anthropic = field(init=False, repr=False)

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
        if not key:
            raise VisionClientError(
                f"{ANTHROPIC_API_KEY_ENV} env var not set — vision tier disabled."
            )
        self._client = anthropic.Anthropic(api_key=key, timeout=self.timeout_sec)

    # ---- public surface --------------------------------------------------

    def call_structured(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Text-only JSON call. Mirrors ``AgentClient.call_structured``."""
        return self._call(
            system_prompt=system_prompt,
            content_blocks=[{"type": "text", "text": user_content}],
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def call_multimodal(
        self,
        *,
        system_prompt: str,
        text_content: str,
        images: Sequence[ImagePart],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """JSON call with one or more attached images.

        Image blocks are placed FIRST (per Anthropic guidance for vision —
        the model attends to images more reliably when they precede the
        text instructions). The trailing text block is what the model
        answers from.
        """
        if not images:
            raise VisionClientError("call_multimodal requires at least one image")
        blocks: list[dict[str, Any]] = [img.to_block(cache=True) for img in images]
        blocks.append({"type": "text", "text": text_content})
        return self._call(
            system_prompt=system_prompt,
            content_blocks=blocks,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    # ---- shared transport ------------------------------------------------

    def _call(
        self,
        *,
        system_prompt: str,
        content_blocks: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        if self.cost_guard is not None and not self.cost_guard.allow():
            raise VisionClientError(
                "daily vision cost cap reached — call blocked by cost_guard"
            )
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
                messages=[{"role": "user", "content": content_blocks}],
            )
        except anthropic.APIError as e:
            raise VisionClientError(f"Anthropic API error: {e}") from e

        if self.cost_guard is not None and getattr(msg, "usage", None) is not None:
            try:
                self.cost_guard.record(msg.usage, model=self.model)
            except Exception:  # noqa: BLE001
                # Cost tracking must never break the actual call.
                pass

        if not msg.content or msg.content[0].type != "text":
            raise VisionClientError(f"unexpected response shape: {msg!r}")
        body = msg.content[0].text.strip()
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
            raise VisionClientError(
                f"response was not valid JSON: {body[:300]}"
            ) from e


# --- convenience constructor matching agents.client style ----------------


def get_vision_client_or_none(
    *, model: str | None = None, cost_guard: Any | None = None,
) -> VisionClient | None:
    """Build a vision client if API key + opt-in toggle allow; else None.

    Honors ``VISION_DISABLED=1`` as a hard kill-switch so the rest of the
    pipeline can call this unconditionally and get ``None`` when the user
    has disabled the tier.
    """
    if os.environ.get("VISION_DISABLED", "").strip() in ("1", "true", "True"):
        return None
    try:
        return VisionClient(
            model=model or DEFAULT_VISION_MODEL,
            cost_guard=cost_guard,
        )
    except VisionClientError:
        return None
