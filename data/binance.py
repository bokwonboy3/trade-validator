"""Binance public Klines fetcher."""
from __future__ import annotations

import json
import time
from typing import Final

import pandas as pd
import requests

KLINES_URL: Final = "https://api.binance.com/api/v3/klines"
FUTURES_FUNDING_URL: Final = "https://fapi.binance.com/fapi/v1/fundingRate"
FUTURES_OI_HIST_URL: Final = "https://fapi.binance.com/futures/data/openInterestHist"
DEFAULT_TIMEOUT: Final = 10  # seconds
_RESP_TEXT_LIMIT: Final = 300

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


def _parse_binance_error(resp: requests.Response) -> str:
    """Extract a friendly message from a Binance error response.

    Binance returns JSON like {"code":-1121,"msg":"Invalid symbol."}; if so,
    show just the msg. Otherwise fall back to a truncated raw body.
    """
    try:
        body = resp.json()
        if isinstance(body, dict) and "msg" in body:
            return f"HTTP {resp.status_code}: {body['msg']}"
    except (ValueError, json.JSONDecodeError):
        pass
    return f"HTTP {resp.status_code}: {resp.text[:_RESP_TEXT_LIMIT]}"


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
        except requests.Timeout:
            last_err = BinanceError(f"Binance request timed out after {timeout}s")
            continue
        except requests.RequestException as e:
            last_err = BinanceError(f"Binance network error: {type(e).__name__}: {e}")
            continue
        if resp.status_code >= 500:
            last_err = BinanceError(_parse_binance_error(resp))
            continue
        if resp.status_code >= 400:
            raise BinanceError(_parse_binance_error(resp))
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
    raise BinanceError(f"Binance fetch failed after 1 retry — {last_err}")


def fetch_funding_rate(
    symbol: str, *, limit: int = 8, timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Recent funding rate history (perpetual futures). Returns list of dicts
    with keys: fundingTime (ms), fundingRate (str, decimal)."""
    params = {"symbol": symbol, "limit": limit}
    try:
        resp = requests.get(FUTURES_FUNDING_URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise BinanceError(f"Binance funding fetch network error: {e}") from e
    if resp.status_code >= 400:
        raise BinanceError(_parse_binance_error(resp))
    return resp.json()


def fetch_open_interest_hist(
    symbol: str, *, period: str = "1h", limit: int = 12, timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Open interest history. `period`: 5m | 15m | 30m | 1h | 2h | 4h | 6h | 12h | 1d.
    Returns list of dicts with sumOpenInterest, sumOpenInterestValue, timestamp."""
    params = {"symbol": symbol, "period": period, "limit": limit}
    try:
        resp = requests.get(FUTURES_OI_HIST_URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise BinanceError(f"Binance OI fetch network error: {e}") from e
    if resp.status_code >= 400:
        raise BinanceError(_parse_binance_error(resp))
    return resp.json()
