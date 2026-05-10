from __future__ import annotations

from pathlib import Path

import pytest

from output.notify import (
    Channel,
    FileChannel,
    StdoutChannel,
    TELEGRAM_CHAT_ID_ENV,
    TELEGRAM_TOKEN_ENV,
    TelegramChannel,
    build_channels,
    dispatch,
)
from scanner_config import (
    FileChannelConfig,
    NotificationsConfig,
    TelegramChannelConfig,
)


def _notif_cfg(channels, telegram_enabled=False, file_path="alerts.log"):
    return NotificationsConfig(
        channels=tuple(channels),
        file=FileChannelConfig(path=file_path),
        telegram=TelegramChannelConfig(enabled=telegram_enabled),
    )


# --- Channel implementations ---
def test_stdout_channel_prints(capsys):
    ch = StdoutChannel()
    ch.send("hello")
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_file_channel_appends(tmp_path):
    p = tmp_path / "alerts.log"
    ch = FileChannel(path=str(p))
    ch.send("alert one")
    ch.send("alert two")
    body = p.read_text()
    assert "alert one" in body
    assert "alert two" in body
    # Separator between alerts
    assert body.count("---") == 2


def test_file_channel_creates_parent_dir(tmp_path):
    nested = tmp_path / "deep" / "alerts.log"
    ch = FileChannel(path=str(nested))
    ch.send("hi")
    assert nested.exists()


def test_telegram_from_env_missing_returns_none(monkeypatch):
    monkeypatch.delenv(TELEGRAM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(TELEGRAM_CHAT_ID_ENV, raising=False)
    assert TelegramChannel.from_env() is None


def test_telegram_from_env_partial_returns_none(monkeypatch):
    monkeypatch.setenv(TELEGRAM_TOKEN_ENV, "abc")
    monkeypatch.delenv(TELEGRAM_CHAT_ID_ENV, raising=False)
    assert TelegramChannel.from_env() is None


def test_telegram_from_env_both_set_returns_instance(monkeypatch):
    monkeypatch.setenv(TELEGRAM_TOKEN_ENV, "abc")
    monkeypatch.setenv(TELEGRAM_CHAT_ID_ENV, "999")
    tg = TelegramChannel.from_env()
    assert tg is not None
    assert tg.bot_token == "abc"
    assert tg.chat_id == "999"


def test_telegram_send_is_stub(capsys, monkeypatch):
    """B5 stub — emits a dry-run line to stderr but does not raise."""
    tg = TelegramChannel(bot_token="abc", chat_id="999")
    tg.send("test message")
    captured = capsys.readouterr()
    assert "telegram stub" in captured.err
    assert "999" in captured.err


# --- build_channels ---
def test_build_channels_stdout_only():
    chans = build_channels(_notif_cfg(["stdout"]))
    assert len(chans) == 1
    assert isinstance(chans[0], StdoutChannel)


def test_build_channels_file_uses_config_path():
    chans = build_channels(_notif_cfg(["file"], file_path="/tmp/x.log"))
    assert len(chans) == 1
    assert isinstance(chans[0], FileChannel)
    assert chans[0].path == "/tmp/x.log"


def test_build_channels_telegram_disabled_skipped(capsys):
    chans = build_channels(_notif_cfg(["telegram"], telegram_enabled=False))
    assert chans == []
    captured = capsys.readouterr()
    assert "disabled in config" in captured.err


def test_build_channels_telegram_enabled_no_env_skipped(monkeypatch, capsys):
    monkeypatch.delenv(TELEGRAM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(TELEGRAM_CHAT_ID_ENV, raising=False)
    chans = build_channels(_notif_cfg(["telegram"], telegram_enabled=True))
    assert chans == []
    captured = capsys.readouterr()
    assert "env vars not set" in captured.err


def test_build_channels_telegram_enabled_with_env(monkeypatch):
    monkeypatch.setenv(TELEGRAM_TOKEN_ENV, "abc")
    monkeypatch.setenv(TELEGRAM_CHAT_ID_ENV, "999")
    chans = build_channels(_notif_cfg(["telegram"], telegram_enabled=True))
    assert len(chans) == 1
    assert isinstance(chans[0], TelegramChannel)


def test_build_channels_combined():
    chans = build_channels(_notif_cfg(["stdout", "file"], file_path="x.log"))
    names = [c.name for c in chans]
    assert names == ["stdout", "file"]


# --- dispatch ---
def test_dispatch_returns_per_channel_results(tmp_path):
    p = tmp_path / "log.txt"
    chans = [StdoutChannel(), FileChannel(path=str(p))]
    results = dispatch(chans, "hello")
    assert results == {"stdout": True, "file": True}
    assert "hello" in p.read_text()


class _BoomChannel:
    name = "boom"

    def send(self, message: str) -> None:
        raise RuntimeError("intentional")


def test_dispatch_isolates_failures(capsys):
    chans = [StdoutChannel(), _BoomChannel()]  # type: ignore[list-item]
    results = dispatch(chans, "hello")
    assert results == {"stdout": True, "boom": False}
    captured = capsys.readouterr()
    assert "boom failed" in captured.err
    assert "hello" in captured.out  # stdout still ran


def test_channel_protocol_runtime_checkable():
    assert isinstance(StdoutChannel(), Channel)
    assert isinstance(FileChannel(path="x"), Channel)
