"""Binance public Klines fetcher."""
from __future__ import annotations

import time
from typing import Final

import pandas as pd
import requests

KLINES_URL: Final = "https://api.binance.com/api/v3/klines"
DEFAULT_TIMEOUT: Final = 10  # seconds

# Binance kline columns (12 fields)
_COLS = [
    "openTime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "closeTime",
    "quoteAssetVolume",
    "numberOfTrades",
    "takerBuyBaseAssetVolume",
    "takerBuyQuoteAssetVolume",
    "ignore",
]
_NUMERIC_COLS = ["open", "high", "low", "close", "volume"]


class BinanceError(RuntimeError):
    pass


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    drop_unclosed: bool = True,
) -> pd.DataFrame:
    """Fetch klines, return DataFrame with closed candles only by default.

    Retries once on 5xx / network error. 4xx surfaces with the Binance message.
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.get(KLINES_URL, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            continue
        if resp.status_code >= 500:
            last_err = BinanceError(f"Binance {resp.status_code}: {resp.text[:200]}")
            continue
        if resp.status_code >= 400:
            raise BinanceError(f"Binance {resp.status_code}: {resp.text[:500]}")
        rows = resp.json()
        df = pd.DataFrame(rows, columns=_COLS)
        for c in _NUMERIC_COLS:
            df[c] = pd.to_numeric(df[c])
        df["openTime"] = df["openTime"].astype("int64")
        df["closeTime"] = df["closeTime"].astype("int64")
        if drop_unclosed and len(df) > 0:
            now_ms = int(time.time() * 1000)
            if df.iloc[-1]["closeTime"] >= now_ms:
                df = df.iloc[:-1].reset_index(drop=True)
        return df
    raise BinanceError(f"Binance fetch failed after retry: {last_err}")
