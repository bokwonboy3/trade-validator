"""Live Binance API regression tests.

Skipped by default. Run with: pytest -m live  (or --run-live)
이 테스트들은 실제 Binance 응답 스키마가 우리 코드가 가정하는 모양과
계속 일치하는지 확인합니다. Binance가 schema를 바꾸면 여기서 깨집니다.
"""
from __future__ import annotations

import time

import pytest

from data.binance import fetch_klines

pytestmark = pytest.mark.live

EXPECTED_COLS = {"openTime", "open", "high", "low", "close", "volume", "closeTime"}


@pytest.mark.parametrize(
    "interval,limit",
    [
        ("4h", 100),
        ("1h", 100),
        ("15m", 200),
    ],
)
def test_live_btcusdt_schema(interval: str, limit: int):
    """Schema + types + reasonable row count + latency."""
    t0 = time.time()
    df = fetch_klines("BTCUSDT", interval, limit)
    elapsed = time.time() - t0

    # drop_unclosed=True can drop the in-progress candle, so allow limit or limit-1
    assert len(df) in (limit, limit - 1), f"got {len(df)} rows for {interval} limit={limit}"
    assert EXPECTED_COLS.issubset(df.columns), f"missing cols: {EXPECTED_COLS - set(df.columns)}"

    # Numeric columns must be float
    for c in ("open", "high", "low", "close", "volume"):
        assert df[c].dtype.kind == "f", f"{interval} {c} dtype={df[c].dtype}"

    # Time columns must be int (ms)
    assert df["openTime"].dtype.kind == "i"
    assert df["closeTime"].dtype.kind == "i"

    # Sanity: positive prices
    assert (df["close"] > 0).all()
    assert (df["high"] >= df["low"]).all()

    # Latency budget: < 5s end-to-end (single fetch)
    assert elapsed < 5.0, f"{interval} fetch took {elapsed:.2f}s"


def test_live_invalid_symbol_raises_with_friendly_message():
    """Real 4xx from Binance is parsed cleanly."""
    from data.binance import BinanceError

    with pytest.raises(BinanceError) as exc:
        fetch_klines("DOESNOTEXIST", "1h", 5)
    msg = str(exc.value)
    # _parse_binance_error extracts the "msg" field from Binance's JSON
    assert "Invalid symbol" in msg or "HTTP 400" in msg


def test_live_drop_unclosed_actually_drops():
    """The most recent fetched candle should already be closed (closeTime < now)."""
    df = fetch_klines("BTCUSDT", "1m", 5)
    if len(df) == 0:
        pytest.skip("no candles returned (rare boundary)")
    now_ms = int(time.time() * 1000)
    assert df.iloc[-1]["closeTime"] < now_ms, "last candle should have closed before now"
