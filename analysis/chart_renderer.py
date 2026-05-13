"""Composite multi-timeframe chart renderer for vision agents (Phase 8c).

Produces a single PNG that stacks 4H / 1H / 15m candles in one image so a
multimodal model gets the full higher-timeframe context, the working
timeframe, and the entry timeframe in one look.

Layout (top → bottom):
    4H : price (candles + MA25 + MA99 + swing markers) | volume | RSI(14)
    1H : same
    15m: same  + optional Entry / SL / TP horizontal lines

Public surface is one function:
    render_composite_chart(df_4h, df_1h, df_15m, *, levels=None) -> str  # base64 PNG

Design notes:
- Pure function. No I/O beyond reading inputs; returns bytes encoded as
  base64 (no temp file on disk) so callers can feed it straight into a
  multimodal API payload.
- Uses mplfinance external-axes mode so we get one figure with a precise
  pixel size (1024×1600) regardless of timeframe candle counts.
- Insufficient data on any sub-frame surfaces as an empty / partial panel
  rather than a hard error — the higher-timeframe context is still useful.

Parity with Binance / TradingView — what matches and what doesn't:

  MATCHES exactly:
    - OHLCV values: pulled from Binance's own klines API.
    - MA25, MA99: simple rolling means on close, same formula as Binance.
    - RSI(14): Wilder-smoothed (book p.65 reference parity, see
      ``test_rsi_matches_wilder_canonical_reference``).
    - Candle coloring: green when close > open, red otherwise.

  INTENTIONAL DEVIATIONS:
    - Forming (unclosed) candle is DROPPED before render
      (``fetch_klines(drop_unclosed=True)``). The framework scores on
      closed candles, so the chart reflects exactly what Tier-1 saw.
    - MA7 is NOT drawn — the trading framework only uses MA25 / MA99
      for alignment, so omitting MA7 keeps the chart less cluttered.
    - Swing-high (▼) and swing-low (▲) markers are OUR overlay
      (n=5 local-pivot detection). Binance doesn't draw these natively;
      the vision system prompts call them out so the model uses them as
      structural anchors rather than mistaking them for Binance marks.
    - Time axis is UTC. Binance UI defaults to the operator's local time;
      using UTC keeps the rendered image deterministic for caching.

  COSMETIC (no material impact on interpretation):
    - mplfinance "yahoo" style colors (same convention as Binance).
    - Date tick label format and candle spacing differ from Binance UI.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Final

import matplotlib

matplotlib.use("Agg")  # headless; never opens a window
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from analysis.levels import find_swings

# Target dimensions: 1024 wide × 1600 tall. At dpi=100, figsize = 10.24 × 16.0.
TARGET_WIDTH_PX: Final = 1024
TARGET_HEIGHT_PX: Final = 1600
DPI: Final = 100
FIGSIZE: Final = (TARGET_WIDTH_PX / DPI, TARGET_HEIGHT_PX / DPI)

MA_FAST: Final = 25
MA_SLOW: Final = 99
RSI_PERIOD: Final = 14
SWING_N: Final = 5
PANEL_RATIOS: Final = (6, 1, 2)  # price / volume / rsi within each timeframe
TIMEFRAME_RATIO: Final = (1, 1, 1)  # 4h / 1h / 15m equal vertical weight


@dataclass(frozen=True)
class TradeLevels:
    """Optional horizontal overlay levels for the 15m panel."""

    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


# --- indicator helpers (kept local so the public API stays minimal) ------


def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder-smoothed RSI — matches the TradingView / Binance default.

    Why Wilder (not SMA-rolling-mean): Binance's chart panel uses Wilder's
    smoothing, which is what traders see. An SMA-smoothed RSI diverges
    from Binance by 5–10 points in trending markets, which would make the
    vision model's reading disagree with what the operator sees.

    Standard Wilder formula (from "New Concepts in Technical Trading
    Systems", 1978):
      - Day `period`: avg = simple mean of the first `period` values.
      - Day t > period: avg_t = (avg_{t-1} * (period-1) + x_t) / period.

    Note: ``pandas.Series.ewm(alpha=1/period, adjust=False)`` performs
    the recursive smoothing but does NOT use the SMA seed Wilder
    prescribes — its first value equals the first raw input. We
    therefore implement the seed-then-recurse loop explicitly to match
    Binance / TradingView exactly. Verified against Wilder's own
    published example (book p.65, 19-row series).
    """
    import numpy as np  # local — keep module-level imports minimal

    delta = close.diff()
    gain = delta.clip(lower=0).to_numpy(dtype=float)
    loss = (-delta.clip(upper=0)).to_numpy(dtype=float)
    n = len(close)
    rsi = np.full(n, np.nan, dtype=float)
    if n <= period:
        return pd.Series(rsi, index=close.index)

    # Wilder seed: index `period` (0-based) uses the SIMPLE mean of the
    # first `period` gain/loss values (indices 1..period inclusive, since
    # delta[0] is NaN).
    avg_gain = float(np.mean(gain[1 : period + 1]))
    avg_loss = float(np.mean(loss[1 : period + 1]))
    rsi[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    # Recursive smoothing from index period+1 onward.
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return pd.Series(rsi, index=close.index)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a Binance-style frame into mplfinance's required shape:

    - DatetimeIndex (UTC) derived from ``openTime`` (ms epoch).
    - Capitalised OHLCV columns.
    """
    if len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    out = pd.DataFrame(
        {
            "Open": df["open"].astype(float).to_numpy(),
            "High": df["high"].astype(float).to_numpy(),
            "Low": df["low"].astype(float).to_numpy(),
            "Close": df["close"].astype(float).to_numpy(),
            "Volume": df["volume"].astype(float).to_numpy(),
        }
    )
    out.index = pd.to_datetime(df["openTime"].to_numpy(), unit="ms", utc=True)
    out.index.name = "Date"
    return out


def _swing_overlays(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Two series (highs / lows) aligned to df.index, NaN except at swing rows.

    Plotted as scatter via mpf.make_addplot to mark structure on the chart.
    """
    n = len(df)
    highs = pd.Series([float("nan")] * n, index=df.index)
    lows = pd.Series([float("nan")] * n, index=df.index)
    if n < 2 * SWING_N + 1:
        return highs, lows
    # find_swings expects lowercase columns — give it the raw frame view.
    raw = pd.DataFrame(
        {"high": df["High"].to_numpy(), "low": df["Low"].to_numpy()},
    )
    for s in find_swings(raw, n=SWING_N):
        if s.kind == "high":
            highs.iloc[s.index] = s.price
        else:
            lows.iloc[s.index] = s.price
    return highs, lows


# --- single-timeframe panel renderer -------------------------------------


def _render_panel(
    df_raw: pd.DataFrame,
    *,
    label: str,
    ax_price,
    ax_volume,
    ax_rsi,
    hlines: list[tuple[float, str, str]] | None = None,
) -> None:
    """Render one timeframe (price + volume + RSI) onto the given axes.

    ``hlines`` is a list of (price, color, label) tuples drawn on ax_price.
    Empty / tiny frames render an "insufficient data" message instead of
    crashing — keeps the composite useful even mid-warmup.
    """
    df = _prepare(df_raw)
    if len(df) == 0:
        for ax, name in ((ax_price, "price"), (ax_volume, "volume"), (ax_rsi, "RSI")):
            ax.set_axis_off()
            ax.text(
                0.5, 0.5, f"{label} {name}: no data",
                ha="center", va="center", transform=ax.transAxes, fontsize=9,
            )
        return

    # Indicator addplots — only include when enough data exists.
    addplots: list = []
    if len(df) >= MA_FAST:
        ma_fast = df["Close"].rolling(MA_FAST).mean()
        addplots.append(mpf.make_addplot(ma_fast, ax=ax_price, color="#ff8c00", width=1.0))
    if len(df) >= MA_SLOW:
        ma_slow = df["Close"].rolling(MA_SLOW).mean()
        addplots.append(mpf.make_addplot(ma_slow, ax=ax_price, color="#2070ff", width=1.0))

    highs, lows = _swing_overlays(df)
    if highs.notna().any():
        addplots.append(
            mpf.make_addplot(
                highs, ax=ax_price, type="scatter", marker="v", markersize=40, color="#cc0000",
            )
        )
    if lows.notna().any():
        addplots.append(
            mpf.make_addplot(
                lows, ax=ax_price, type="scatter", marker="^", markersize=40, color="#008800",
            )
        )

    rsi = _rsi(df["Close"])
    addplots.append(mpf.make_addplot(rsi, ax=ax_rsi, color="#7030a0", width=1.0))

    mpf.plot(
        df,
        type="candle",
        ax=ax_price,
        volume=ax_volume,
        addplot=addplots,
        style="yahoo",
        datetime_format="%m-%d %H:%M",
        xrotation=0,
        warn_too_much_data=10000,
        update_width_config={"candle_linewidth": 0.6},
    )

    # Optional entry/SL/TP overlays.
    if hlines:
        for price, color, lbl in hlines:
            ax_price.axhline(price, color=color, linewidth=1.0, linestyle="--", alpha=0.9)
            ax_price.text(
                df.index[-1], price, f" {lbl} {price:g}",
                color=color, fontsize=8, va="center",
            )

    # RSI guides at 30 / 70.
    ax_rsi.axhline(70, color="#aaaaaa", linewidth=0.6, linestyle=":")
    ax_rsi.axhline(30, color="#aaaaaa", linewidth=0.6, linestyle=":")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", fontsize=8)

    ax_price.set_title(
        f"{label}  (MA{MA_FAST}=orange · MA{MA_SLOW}=blue · swings ▲▼)",
        fontsize=10, loc="left",
    )


# --- public entry point --------------------------------------------------


def render_composite_chart(
    df_4h: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    *,
    levels: TradeLevels | None = None,
) -> str:
    """Render a 4H + 1H + 15m composite chart, return base64-encoded PNG.

    The 15m panel optionally shows ``levels.entry`` / ``stop_loss`` /
    ``take_profit`` as dashed horizontal lines.
    """
    fig = plt.figure(figsize=FIGSIZE, dpi=DPI)

    # 3 timeframes × 3 sub-panels each = 9 rows in one GridSpec.
    height_ratios: list[int] = []
    for tf_w in TIMEFRAME_RATIO:
        height_ratios.extend(r * tf_w for r in PANEL_RATIOS)
    gs = fig.add_gridspec(
        nrows=9, ncols=1, height_ratios=height_ratios, hspace=0.45,
    )
    axes = [fig.add_subplot(gs[i, 0]) for i in range(9)]

    panels = [
        (df_4h, "4H", axes[0], axes[1], axes[2], None),
        (df_1h, "1H", axes[3], axes[4], axes[5], None),
        (
            df_15m,
            "15m",
            axes[6],
            axes[7],
            axes[8],
            _hlines_from_levels(levels),
        ),
    ]
    for df, label, ax_p, ax_v, ax_r, hlines in panels:
        _render_panel(
            df, label=label, ax_price=ax_p, ax_volume=ax_v, ax_rsi=ax_r, hlines=hlines,
        )

    buf = io.BytesIO()
    # NOTE: NO ``bbox_inches="tight"`` — mplfinance lays out very wide
    # auto-formatted date tick labels per panel; combined with tight-bbox
    # recomputation, matplotlib has been observed to balloon the figure
    # width by ~100×. Fix the canvas at the configured FIGSIZE instead.
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _hlines_from_levels(levels: TradeLevels | None) -> list[tuple[float, str, str]] | None:
    if levels is None:
        return None
    out: list[tuple[float, str, str]] = []
    if levels.entry is not None:
        out.append((float(levels.entry), "#0066cc", "Entry"))
    if levels.stop_loss is not None:
        out.append((float(levels.stop_loss), "#cc0000", "SL"))
    if levels.take_profit is not None:
        out.append((float(levels.take_profit), "#008800", "TP"))
    return out or None
