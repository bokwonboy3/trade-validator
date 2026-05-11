from pathlib import Path

import pytest

from scanner_config import (
    ConfigError,
    EXAMPLE_CONFIG_PATH,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_example_config_loads():
    """The shipped example must always parse cleanly."""
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert "BTCUSDT" in cfg.symbols
    assert cfg.thresholds.min_score == 4
    assert cfg.thresholds.default_rr == 3.0
    assert "stdout" in cfg.notifications.channels
    assert cfg.notifications.telegram.enabled is False


def test_load_minimal_valid(tmp_path):
    p = _write(
        tmp_path,
        """
[scanner]
symbols = ["BTCUSDT"]

[scanner.thresholds]
min_score = 4
default_rr = 3.0

[notifications]
channels = ["stdout"]
""",
    )
    cfg = load_config(p)
    assert cfg.symbols == ("BTCUSDT",)
    # File/Telegram defaults applied
    assert cfg.notifications.file.path == "alerts.log"
    assert cfg.notifications.telegram.enabled is False


def test_missing_scanner_table(tmp_path):
    p = _write(tmp_path, """[notifications]\nchannels=["stdout"]\n""")
    with pytest.raises(ConfigError, match=r"\[scanner\]"):
        load_config(p)


def test_empty_symbols_fails(tmp_path):
    p = _write(
        tmp_path,
        """
[scanner]
symbols = []

[scanner.thresholds]
min_score = 4
default_rr = 3.0

[notifications]
channels = ["stdout"]
""",
    )
    with pytest.raises(ConfigError, match="symbols"):
        load_config(p)


def test_invalid_min_score(tmp_path):
    p = _write(
        tmp_path,
        """
[scanner]
symbols = ["BTCUSDT"]

[scanner.thresholds]
min_score = 7
default_rr = 3.0

[notifications]
channels = ["stdout"]
""",
    )
    with pytest.raises(ConfigError, match="min_score"):
        load_config(p)


def test_unknown_channel(tmp_path):
    p = _write(
        tmp_path,
        """
[scanner]
symbols = ["BTCUSDT"]

[scanner.thresholds]
min_score = 4
default_rr = 3.0

[notifications]
channels = ["smoke-signals"]
""",
    )
    with pytest.raises(ConfigError, match="smoke-signals"):
        load_config(p)


def test_default_rr_must_be_positive(tmp_path):
    p = _write(
        tmp_path,
        """
[scanner]
symbols = ["BTCUSDT"]

[scanner.thresholds]
min_score = 4
default_rr = 0

[notifications]
channels = ["stdout"]
""",
    )
    with pytest.raises(ConfigError, match="default_rr"):
        load_config(p)


def test_missing_file_returns_friendly_error(tmp_path):
    nonexistent = tmp_path / "nope.toml"
    with pytest.raises(ConfigError, match="config not found"):
        load_config(nonexistent)
