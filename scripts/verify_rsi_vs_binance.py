"""Ground-truth verification helper for RSI parity with Binance UI.

Why this script exists
----------------------
The chart_renderer ships its RSI(14) panel as "Wilder-smoothed, matching
Binance / TradingView". That claim is supported by:
  - parity with J.W. Wilder's 1978 book reference (test_chart_renderer)
  - parity with the `ta` library (>=170 bars warmup) on the trailing tail

But neither of those is Binance itself. Only a human looking at the
Binance UI can close that loop. This script prints our RSI(14) value
for the last few CLOSED 1H BTC candles so the operator can:

  1. Open https://www.binance.com/en/trade/BTC_USDT (or any spot pair).
  2. Switch the chart interval to 1h.
  3. Hover the most recent CLOSED candle (NOT the one currently forming).
  4. Read the RSI(14) panel value at that timestamp.
  5. Compare to this script's output. Difference < 0.05 = parity confirmed.

Usage:
    python scripts/verify_rsi_vs_binance.py [SYMBOL] [INTERVAL] [N_TAIL]

Defaults: BTCUSDT, 1h, 5 candles.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Allow running as `python scripts/verify_rsi_vs_binance.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.chart_renderer import _rsi  # noqa: E402
from data.binance import fetch_klines  # noqa: E402


def main(argv: list[str]) -> int:
    symbol = argv[1] if len(argv) > 1 else "BTCUSDT"
    interval = argv[2] if len(argv) > 2 else "1h"
    n_tail = int(argv[3]) if len(argv) > 3 else 5

    # Warmup of 200 bars is more than enough for Wilder smoothing to be
    # fully converged regardless of seed convention.
    df = fetch_klines(symbol, interval, 200)
    closes = pd.Series(df["close"].astype(float).to_numpy())
    rsi = _rsi(closes, period=14).to_numpy()

    print(f"{symbol} {interval} — last {n_tail} CLOSED candles (UTC)")
    print(f"{'closeTime (UTC)':<22} {'close':>12} {'our RSI(14)':>14}")
    print("-" * 50)
    for i in range(len(df) - n_tail, len(df)):
        ts = datetime.fromtimestamp(df["closeTime"].iloc[i] / 1000, tz=timezone.utc)
        print(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S'):<22} "
            f"{closes.iloc[i]:>12.2f} {rsi[i]:>14.4f}"
        )

    print()
    print("Next steps:")
    print("  1. Open Binance's chart for this symbol at the same interval.")
    print("  2. Enable the RSI(14) indicator.")
    print("  3. Hover the matching CLOSED candle timestamp.")
    print("  4. Compare values. Drift < 0.05 = parity confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
