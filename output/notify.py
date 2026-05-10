"""Notification dispatch — pluggable channels (stdout, file, telegram).

Each channel implements `Channel.send(message)`. `build_channels()` constructs
the channel list from a NotificationsConfig, and `dispatch()` sends a message
to all of them, logging (but not crashing on) per-channel failures.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from scanner_config import NotificationsConfig

TELEGRAM_TOKEN_ENV: Final = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV: Final = "TELEGRAM_CHAT_ID"


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
    """Telegram Bot API channel.

    Phase-0/B5 stub: structural placeholder — emits a dry-run log line.
    The real HTTP call is implemented in B6.
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
        # B5 stub — B6 replaces with HTTP POST to api.telegram.org.
        print(
            f"[telegram stub] would send to chat {self.chat_id}: "
            f"{message[:80]}{'...' if len(message) > 80 else ''}",
            file=sys.stderr,
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
