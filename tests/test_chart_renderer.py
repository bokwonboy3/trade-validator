"""Unit tests for analysis.chart_renderer (Phase 8c PR-1)."""
from __future__ import annotations

import base64
import io
import struct

import numpy as np
import pandas as pd
import pytest

from analysis import chart_renderer
from analysis.chart_renderer import (
    FIGSIZE,
    TARGET_HEIGHT_PX,
    TARGET_WIDTH_PX,
    TradeLevels,
    render_composite_chart,
)


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    """Parse width/height out of the PNG IHDR chunk (offsets 16-23)."""
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def _synthetic(n: int, step_ms: int, *, base: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Make a Binance-shaped klines frame with smooth random-walk closes."""
    rng = np.random.default_rng(seed)
    closes = base + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + rng.uniform(0.1, 0.4, n)
    lows = np.minimum(opens, closes) - np.maximum(rng.uniform(0.1, 0.4, n), 0.01)
    vol = rng.uniform(50, 200, n)
    open_ts = np.arange(n, dtype="int64") * step_ms + 1_700_000_000_000
    return pd.DataFrame(
        {
            "openTime": open_ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vol,
            "closeTime": open_ts + step_ms - 1,
        }
    )


# --- happy path -----------------------------------------------------------


def test_render_returns_base64_png_at_target_size():
    df_4h = _synthetic(120, 4 * 3600 * 1000)
    df_1h = _synthetic(120, 3600 * 1000, seed=7)
    df_15m = _synthetic(120, 15 * 60 * 1000, seed=11)
    b64 = render_composite_chart(df_4h, df_1h, df_15m)
    raw = base64.b64decode(b64)
    assert raw.startswith(b"\x89PNG\r\n\x1a\n"), "output must be a valid PNG"
    # Sanity: image is non-trivial in size (>50 KB for our 1024x1600 target).
    assert len(raw) > 50_000


def test_render_actual_pixel_dimensions_exact():
    """Regression: matplotlib's ``bbox_inches="tight"`` was observed to
    balloon the canvas to ~99K × 1.3K px when mplfinance laid out wide date
    tick labels. The renderer must hold to the configured FIGSIZE × DPI."""
    df_4h = _synthetic(80, 4 * 3600 * 1000)
    df_1h = _synthetic(80, 3600 * 1000, seed=7)
    df_15m = _synthetic(80, 15 * 60 * 1000, seed=11)
    raw = base64.b64decode(
        render_composite_chart(
            df_4h, df_1h, df_15m,
            levels=TradeLevels(entry=100.5, stop_loss=98.0, take_profit=105.0),
        )
    )
    w, h = _png_dimensions(raw)
    assert (w, h) == (TARGET_WIDTH_PX, TARGET_HEIGHT_PX), (
        f"PNG dims drifted: got {w}x{h}, expected "
        f"{TARGET_WIDTH_PX}x{TARGET_HEIGHT_PX}"
    )


def test_target_dimensions_constants_match():
    # Guards against accidental edits drifting the figure size away from
    # the agreed 1024×1600 spec.
    assert TARGET_WIDTH_PX == 1024
    assert TARGET_HEIGHT_PX == 1600
    assert FIGSIZE == (10.24, 16.0)


def test_render_with_trade_levels_overlays_lines():
    df_4h = _synthetic(80, 4 * 3600 * 1000)
    df_1h = _synthetic(80, 3600 * 1000, seed=7)
    df_15m = _synthetic(80, 15 * 60 * 1000, seed=11)
    b64 = render_composite_chart(
        df_4h,
        df_1h,
        df_15m,
        levels=TradeLevels(entry=100.5, stop_loss=97.0, take_profit=106.0),
    )
    # Just confirm it renders without raising — exact pixel checks are brittle.
    assert base64.b64decode(b64).startswith(b"\x89PNG")


# --- robustness -----------------------------------------------------------


def test_empty_frame_is_tolerated():
    """Renderer should not crash on a missing timeframe — empty panels
    are acceptable since higher-timeframe context is still useful."""
    empty = pd.DataFrame(
        columns=["openTime", "open", "high", "low", "close", "volume", "closeTime"]
    )
    df_1h = _synthetic(80, 3600 * 1000)
    df_15m = _synthetic(80, 15 * 60 * 1000, seed=2)
    b64 = render_composite_chart(empty, df_1h, df_15m)
    assert base64.b64decode(b64).startswith(b"\x89PNG")


def test_short_frame_below_ma_window_is_tolerated():
    """Frame shorter than MA99 — MA overlay must just be skipped, not crash."""
    df_4h = _synthetic(30, 4 * 3600 * 1000)  # < 99
    df_1h = _synthetic(80, 3600 * 1000)
    df_15m = _synthetic(80, 15 * 60 * 1000, seed=2)
    b64 = render_composite_chart(df_4h, df_1h, df_15m)
    assert base64.b64decode(b64).startswith(b"\x89PNG")


# --- helper modules -------------------------------------------------------


def test_rsi_helper_in_unit_range():
    closes = pd.Series(np.linspace(100, 110, 30) + np.random.default_rng(0).normal(0, 0.5, 30))
    rsi = chart_renderer._rsi(closes)
    valid = rsi.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_rsi_matches_wilder_canonical_reference():
    """Compare against Wilder's own published 14-period example (book p.65).

    Locks in the calculation to ensure parity with Binance / TradingView —
    those platforms use Wilder smoothing, and the vision model must see
    the same RSI values the operator sees on Binance's chart panel.
    """
    closes = pd.Series([
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
        46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22,
    ])
    rsi = chart_renderer._rsi(closes, period=14).to_numpy()
    # Published Wilder reference values for indices 14..18 inclusive.
    reference = [70.46, 66.25, 66.48, 69.35, 66.29]
    for i, expected in enumerate(reference, start=14):
        assert rsi[i] == pytest.approx(expected, abs=0.01), (
            f"RSI[{i}] = {rsi[i]:.4f}, expected ~{expected} (Wilder reference)"
        )


def test_rsi_seed_is_at_index_period_not_earlier():
    """First non-NaN should appear exactly at index ``period`` — earlier
    indices have no Wilder seed yet and must remain NaN."""
    closes = pd.Series(np.linspace(100, 110, 30))
    rsi = chart_renderer._rsi(closes, period=14)
    # All NaN up to index 13; first defined value at index 14.
    assert rsi.iloc[:14].isna().all()
    assert not pd.isna(rsi.iloc[14])


def test_trade_levels_to_hlines_skips_none():
    out = chart_renderer._hlines_from_levels(TradeLevels(entry=100.0))
    assert out is not None
    assert len(out) == 1 and out[0][2] == "Entry"
    assert chart_renderer._hlines_from_levels(None) is None
    assert chart_renderer._hlines_from_levels(TradeLevels()) is None
