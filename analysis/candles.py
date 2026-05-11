"""Candle pattern recognition + volume spike detection.

A "candle" here is a mapping with keys: open, high, low, close, volume.
Pure functions — no I/O, no DataFrame mutation.
"""
from __future__ import annotations

from typing import Final, Mapping

# Public type alias — anywhere a single OHLCV candle dict is expected, use this.
Candle = Mapping[str, float]

VOLUME_MULTIPLIER: Final[float] = 1.5


def _body(c: Candle) -> float:
    return abs(c["close"] - c["open"])


def _upper_wick(c: Candle) -> float:
    return c["high"] - max(c["open"], c["close"])


def _lower_wick(c: Candle) -> float:
    return min(c["open"], c["close"]) - c["low"]


def is_hammer(candle: Candle) -> bool:
    """Hammer: lower_wick > 2×body AND upper_wick < body.

    Body of zero (doji) is rejected — both conditions become trivially true
    or comparison-based, which gives a false signal. Require body > 0.
    """
    body = _body(candle)
    if body <= 0:
        return False
    return _lower_wick(candle) > 2 * body and _upper_wick(candle) < body


def is_shooting_star(candle: Candle) -> bool:
    body = _body(candle)
    if body <= 0:
        return False
    return _upper_wick(candle) > 2 * body and _lower_wick(candle) < body


def is_volume_spike(
    volume: float, prev_volumes: list[float], multiplier: float = VOLUME_MULTIPLIER
) -> bool:
    """True if ``volume`` exceeds ``multiplier`` × average of ``prev_volumes``.

    Returns False if there are no previous volumes (cannot compute baseline).
    """
    if not prev_volumes:
        return False
    avg = sum(prev_volumes) / len(prev_volumes)
    if avg <= 0:
        return False
    return volume > avg * multiplier


def is_rejection(candle: Candle, direction: str) -> bool:
    """Direction-aware rejection: hammer for long, shooting star for short."""
    d = direction.lower()
    if d == "long":
        return is_hammer(candle)
    if d == "short":
        return is_shooting_star(candle)
    raise ValueError(f"unknown direction: {direction}")
