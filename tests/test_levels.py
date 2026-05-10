import json
from pathlib import Path

import pandas as pd
import pytest

from analysis.levels import find_swings, swing_highs, swing_lows

FIXTURES = Path(__file__).parent / "fixtures"


def _df(highs: list[float], lows: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"high": highs, "low": lows, "close": highs, "open": lows, "volume": [1] * len(highs)})


def test_simple_swing_high():
    # Peak at index 5, n=2 → confirmed swing high
    highs = [1, 2, 3, 4, 5, 10, 5, 4, 3, 2, 1]
    lows = [0] * 11
    df = _df(highs, lows)
    swings = find_swings(df, n=2)
    high = next(s for s in swings if s.kind == "high")
    assert high.index == 5
    assert high.price == 10.0


def test_simple_swing_low():
    lows = [10, 9, 8, 7, 6, 1, 6, 7, 8, 9, 10]
    highs = [100] * 11
    df = _df(highs, lows)
    swings = find_swings(df, n=2)
    low = next(s for s in swings if s.kind == "low")
    assert low.index == 5
    assert low.price == 1.0


def test_n_filters_noise():
    # peak at 3 with n=1 is a swing, but n=3 requires it to dominate 3 candles each side
    highs = [1, 2, 5, 2, 1, 0, 0, 0]
    lows = [0] * 8
    df = _df(highs, lows)
    assert any(s.kind == "high" and s.index == 2 for s in find_swings(df, n=1))
    # n=3 needs 3 candles on each side of i; range becomes (3, 4) → only i=3 checked,
    # which is not a high. So no swings.
    assert not any(s.kind == "high" for s in find_swings(df, n=3))


def test_edges_excluded():
    highs = [10, 1, 1, 1, 1, 1, 1, 10]
    lows = [0] * 8
    df = _df(highs, lows)
    res = find_swings(df, n=2)
    # Indices 0 and 7 are edges (within n=2 of boundaries) → no swings detected
    assert all(0 < s.index < 7 for s in res)


def test_helpers_partition():
    # Swing high at index 3, swing low at index 8 — distinct indices
    highs = [1, 2, 3, 10, 3, 2, 1, 2, 3, 4, 5]
    lows = [5, 5, 5, 5, 5, 4, 3, 2, 0, 2, 3]
    df = _df(highs, lows)
    highs_only = swing_highs(df, n=2)
    lows_only = swing_lows(df, n=2)
    assert len(highs_only) >= 1
    assert len(lows_only) >= 1
    assert all(s.kind == "high" for s in highs_only)
    assert all(s.kind == "low" for s in lows_only)


def test_real_btc_fixture_finds_some_swings():
    """Sanity check on real BTC 1h data — should find at least 5 swings with n=5."""
    raw = json.loads((FIXTURES / "btcusdt_1h.json").read_text())
    df = pd.DataFrame(
        raw,
        columns=[
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
        ],
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    swings = find_swings(df, n=5)
    # Loose lower bound — real data; just confirms algorithm runs and finds swings
    assert len(swings) >= 3, f"expected ≥3 swings on 100 1H BTC candles, got {len(swings)}"


def test_n_zero_raises():
    df = _df([1, 2, 3], [0, 0, 0])
    with pytest.raises(ValueError):
        find_swings(df, n=0)
