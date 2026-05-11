"""Notification dispatch — pluggable channels (stdout, file, telegram).

Each channel implements `Channel.send(message)`. `build_channels()` constructs
the channel list from a NotificationsConfig, and `dispatch()` sends a message
to all of them, logging (but not crashing on) per-channel failures.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import requests

from scanner_config import NotificationsConfig

TELEGRAM_TOKEN_ENV: Final = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV: Final = "TELEGRAM_CHAT_ID"
TELEGRAM_API_BASE: Final = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SEC: Final = 10
TELEGRAM_MAX_LEN: Final = 4096  # Bot API hard limit
DEFAULT_JSONL_PATH: Final = "alerts.jsonl"


@runtime_checkable
class Channel(Protocol):
    """A notification destination. Implementations must be best-effort —
    raising from send() is allowed; the dispatcher catches and logs."""

    name: str

    def send(self, message: str) -> None: ...


@dataclass
class StdoutChannel:
    name: str = "stdout"

    def send(self, message: str) -> None:
        print(message)


@dataclass
class FileChannel:
    path: str
    name: str = "file"

    def send(self, message: str) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(message)
            if not message.endswith("\n"):
                f.write("\n")
            f.write("---\n")


@dataclass
class TelegramChannel:
    """Telegram Bot API sendMessage channel.

    Bot token + chat_id come from env vars (TELEGRAM_BOT_TOKEN / _CHAT_ID).
    Messages longer than 4096 chars are truncated with a tail marker.
    """

    bot_token: str
    chat_id: str
    name: str = "telegram"

    @classmethod
    def from_env(cls) -> "TelegramChannel | None":
        """Construct from env vars; return None if either is missing."""
        token = os.environ.get(TELEGRAM_TOKEN_ENV, "").strip()
        chat = os.environ.get(TELEGRAM_CHAT_ID_ENV, "").strip()
        if not token or not chat:
            return None
        return cls(bot_token=token, chat_id=chat)

    def send(self, message: str) -> None:
        text = message
        if len(text) > TELEGRAM_MAX_LEN:
            text = text[: TELEGRAM_MAX_LEN - 20] + "\n... [truncated]"
        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_TIMEOUT_SEC,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Telegram HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise RuntimeError(f"Telegram returned non-JSON: {e}")
        if not body.get("ok"):
            raise RuntimeError(
                f"Telegram rejected: {body.get('description', body)}"
            )


def build_channels(cfg: NotificationsConfig) -> list[Channel]:
    """Construct enabled Channel instances from config.

    Telegram inclusion requires both `cfg.telegram.enabled=true` AND env vars.
    If either condition fails, telegram is silently skipped (with stderr note).
    """
    channels: list[Channel] = []
    for name in cfg.channels:
        if name == "stdout":
            channels.append(StdoutChannel())
        elif name == "file":
            channels.append(FileChannel(path=cfg.file.path))
        elif name == "telegram":
            if not cfg.telegram.enabled:
                print(
                    "[notify] telegram listed in channels but disabled in config — skipped",
                    file=sys.stderr,
                )
                continue
            tg = TelegramChannel.from_env()
            if tg is None:
                print(
                    f"[notify] telegram enabled but {TELEGRAM_TOKEN_ENV}/"
                    f"{TELEGRAM_CHAT_ID_ENV} env vars not set — skipped",
                    file=sys.stderr,
                )
                continue
            channels.append(tg)
    return channels


def append_jsonl(record: dict[str, Any], path: str = DEFAULT_JSONL_PATH) -> None:
    """Append one structured alert record to alerts.jsonl for later analysis."""
    record["ts"] = datetime.now(timezone.utc).isoformat()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def dispatch(channels: list[Channel], message: str) -> dict[str, bool]:
    """Send `message` to each channel. Return per-channel success map.

    Failures from one channel do not affect the others.
    """
    results: dict[str, bool] = {}
    for ch in channels:
        try:
            ch.send(message)
            results[ch.name] = True
        except Exception as e:
            results[ch.name] = False
            print(f"[notify] {ch.name} failed: {type(e).__name__}: {e}", file=sys.stderr)
    return results
