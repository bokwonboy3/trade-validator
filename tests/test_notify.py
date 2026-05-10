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


def test_telegram_send_posts_to_api(mocker):
    fake_resp = mocker.Mock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"ok": True, "result": {}}
    post = mocker.patch("output.notify.requests.post", return_value=fake_resp)

    tg = TelegramChannel(bot_token="TOKEN_X", chat_id="999")
    tg.send("test message")

    post.assert_called_once()
    args, kwargs = post.call_args
    # URL contains bot token
    assert "TOKEN_X" in args[0] or "TOKEN_X" in kwargs.get("url", "")
    # Body has chat_id and text
    payload = kwargs["json"]
    assert payload["chat_id"] == "999"
    assert payload["text"] == "test message"
    assert payload["disable_web_page_preview"] is True


def test_telegram_send_raises_on_http_error(mocker):
    fake_resp = mocker.Mock()
    fake_resp.status_code = 401
    fake_resp.text = '{"ok":false,"description":"Unauthorized"}'
    mocker.patch("output.notify.requests.post", return_value=fake_resp)

    tg = TelegramChannel(bot_token="bad", chat_id="999")
    with pytest.raises(RuntimeError, match="Telegram HTTP 401"):
        tg.send("hi")


def test_telegram_send_raises_on_ok_false(mocker):
    fake_resp = mocker.Mock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"ok": False, "description": "chat not found"}
    mocker.patch("output.notify.requests.post", return_value=fake_resp)

    tg = TelegramChannel(bot_token="good", chat_id="bad")
    with pytest.raises(RuntimeError, match="chat not found"):
        tg.send("hi")


def test_telegram_send_truncates_long_messages(mocker):
    fake_resp = mocker.Mock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"ok": True, "result": {}}
    post = mocker.patch("output.notify.requests.post", return_value=fake_resp)

    long_msg = "x" * 5000
    tg = TelegramChannel(bot_token="t", chat_id="c")
    tg.send(long_msg)

    sent_text = post.call_args.kwargs["json"]["text"]
    assert len(sent_text) <= 4096
    assert sent_text.endswith("[truncated]")


def test_telegram_send_propagates_via_dispatcher_isolation(mocker):
    """Telegram failure shouldn't break other channels."""
    fake_resp = mocker.Mock()
    fake_resp.status_code = 500
    fake_resp.text = "Server Error"
    mocker.patch("output.notify.requests.post", return_value=fake_resp)

    tg = TelegramChannel(bot_token="t", chat_id="c")
    chans = [StdoutChannel(), tg]
    results = dispatch(chans, "msg")
    assert results == {"stdout": True, "telegram": False}


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
