"""Load and validate the scanner config (TOML)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

VALID_CHANNELS: Final = {"stdout", "file", "telegram"}
DEFAULT_CONFIG_PATH: Final = Path("config.toml")
EXAMPLE_CONFIG_PATH: Final = Path("config.example.toml")


class ConfigError(ValueError):
    """Raised when config.toml is missing fields or has invalid values."""


@dataclass(frozen=True)
class FileChannelConfig:
    path: str


@dataclass(frozen=True)
class TelegramChannelConfig:
    enabled: bool


@dataclass(frozen=True)
class NotificationsConfig:
    channels: tuple[Literal["stdout", "file", "telegram"], ...]
    file: FileChannelConfig
    telegram: TelegramChannelConfig


@dataclass(frozen=True)
class ThresholdsConfig:
    min_score: int
    default_rr: float


@dataclass(frozen=True)
class ScannerConfig:
    symbols: tuple[str, ...]
    thresholds: ThresholdsConfig
    notifications: NotificationsConfig


def load_config(path: Path | str | None = None) -> ScannerConfig:
    """Load `config.toml` (or `config.example.toml` as fallback).

    Returns a frozen ScannerConfig. Raises ConfigError on validation failure.
    """
    candidates = [Path(path)] if path else [DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH]
    chosen: Path | None = next((p for p in candidates if p.exists()), None)
    if chosen is None:
        raise ConfigError(
            f"config not found — tried {[str(p) for p in candidates]}; "
            f"copy config.example.toml → config.toml and edit"
        )
    with chosen.open("rb") as f:
        raw = tomllib.load(f)

    return _validate(raw)


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing key '{key}'")
    return d[key]


def _validate(raw: dict) -> ScannerConfig:
    scanner = _require(raw, "scanner", "[scanner]")
    if not isinstance(scanner, dict):
        raise ConfigError("[scanner]: must be a table")

    symbols = _require(scanner, "symbols", "[scanner]")
    if not isinstance(symbols, list) or not all(isinstance(s, str) and s for s in symbols):
        raise ConfigError("[scanner].symbols: must be a non-empty list of strings")
    if len(symbols) == 0:
        raise ConfigError("[scanner].symbols: at least one symbol required")

    thresholds_raw = _require(scanner, "thresholds", "[scanner]")
    if not isinstance(thresholds_raw, dict):
        raise ConfigError("[scanner.thresholds]: must be a table")
    min_score = _require(thresholds_raw, "min_score", "[scanner.thresholds]")
    default_rr = _require(thresholds_raw, "default_rr", "[scanner.thresholds]")
    if not isinstance(min_score, int) or not (0 <= min_score <= 5):
        raise ConfigError("[scanner.thresholds].min_score: int in [0, 5]")
    if not isinstance(default_rr, (int, float)) or default_rr <= 0:
        raise ConfigError("[scanner.thresholds].default_rr: positive number")
    thresholds = ThresholdsConfig(min_score=min_score, default_rr=float(default_rr))

    notif_raw = _require(raw, "notifications", "[notifications]")
    if not isinstance(notif_raw, dict):
        raise ConfigError("[notifications]: must be a table")
    channels = _require(notif_raw, "channels", "[notifications]")
    if not isinstance(channels, list) or not channels:
        raise ConfigError("[notifications].channels: must be a non-empty list")
    invalid = [c for c in channels if c not in VALID_CHANNELS]
    if invalid:
        raise ConfigError(
            f"[notifications].channels: unknown channel(s) {invalid}; "
            f"valid: {sorted(VALID_CHANNELS)}"
        )

    file_raw = notif_raw.get("file", {})
    if not isinstance(file_raw, dict):
        raise ConfigError("[notifications.file]: must be a table")
    file_path = file_raw.get("path", "alerts.log")
    if not isinstance(file_path, str):
        raise ConfigError("[notifications.file].path: must be a string")
    file_cfg = FileChannelConfig(path=file_path)

    tg_raw = notif_raw.get("telegram", {})
    if not isinstance(tg_raw, dict):
        raise ConfigError("[notifications.telegram]: must be a table")
    tg_enabled = tg_raw.get("enabled", False)
    if not isinstance(tg_enabled, bool):
        raise ConfigError("[notifications.telegram].enabled: bool")
    tg_cfg = TelegramChannelConfig(enabled=tg_enabled)

    return ScannerConfig(
        symbols=tuple(symbols),
        thresholds=thresholds,
        notifications=NotificationsConfig(
            channels=tuple(channels),
            file=file_cfg,
            telegram=tg_cfg,
        ),
    )
