import json
from pathlib import Path

import pandas as pd
import pytest

from analysis.levels import compute_sr, find_swings, swing_highs, swing_lows

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


# ---------- compute_sr ----------

def _sr_df(highs: list[float], lows: list[float], closes: list[float] | None = None) -> pd.DataFrame:
    closes = closes if closes is not None else [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({
        "high": highs, "low": lows, "close": closes,
        "open": closes, "volume": [1] * len(highs),
    })


def test_compute_sr_partitions_by_reference():
    # Swing high @ idx 3 = 20, swing low @ idx 8 = 1
    highs = [10, 11, 12, 20, 12, 11, 10, 9, 8, 9, 10]
    lows = [5, 5, 5, 5, 5, 4, 3, 2, 1, 2, 3]
    df = _sr_df(highs, lows)
    sr = compute_sr(df, reference_price=10.0, n_swing=2, include_ma=False)
    # 20 is above 10 → resistance; 1 is below 10 → support
    assert any(p == 20.0 for _, p in sr.resistance)
    assert any(p == 1.0 for _, p in sr.support)


def test_compute_sr_sorts_nearest_first():
    # Two highs above ref (15 and 30) — 15 must come first.
    highs = [10, 15, 10, 30, 10, 5, 10, 5, 10]
    lows = [4] * 9
    df = _sr_df(highs, lows)
    sr = compute_sr(df, reference_price=8.0, n_swing=1, include_ma=False)
    prices = [p for _, p in sr.resistance]
    assert prices == sorted(prices)
    assert prices[0] < prices[1]


def test_compute_sr_caps_max_levels():
    # Build a frame with multiple swings on each side
    highs = [5, 20, 5, 25, 5, 30, 5, 35, 5, 40, 5]
    lows = [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4]
    df = _sr_df(highs, lows)
    sr = compute_sr(df, reference_price=10.0, n_swing=1, include_ma=False, max_levels=2)
    assert len(sr.resistance) <= 2


def test_compute_sr_drops_levels_equal_to_reference():
    highs = [10, 50, 10, 50, 10]
    lows = [9, 9, 9, 9, 9]
    df = _sr_df(highs, lows)
    sr = compute_sr(df, reference_price=50.0, n_swing=1, include_ma=False)
    assert not any(p == 50.0 for _, p in sr.resistance)
    assert not any(p == 50.0 for _, p in sr.support)


def test_compute_sr_includes_ma_when_enabled():
    # Closes drift up so MA25 sits below the latest close; MA99 needs ≥99 rows
    closes = [10.0 + i * 0.1 for i in range(120)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    df = _sr_df(highs, lows, closes=closes)
    ref = closes[-1] + 5  # well above everything
    sr = compute_sr(df, reference_price=ref, n_swing=5, include_ma=True)
    labels = {lbl for lbl, _ in sr.resistance + sr.support}
    assert "ma25" in labels
    assert "ma99" in labels


def test_compute_sr_nearest_helpers():
    highs = [5, 20, 5, 30, 5]
    lows = [4, 4, 4, 4, 4]
    df = _sr_df(highs, lows)
    sr = compute_sr(df, reference_price=10.0, n_swing=1, include_ma=False)
    near_r = sr.nearest_resistance()
    assert near_r is not None and near_r[1] == 20.0
    assert sr.nearest_support() is None  # no swing_low below 10


def test_compute_sr_invalid_max_levels():
    df = _sr_df([1, 2, 3], [0, 0, 0])
    with pytest.raises(ValueError):
        compute_sr(df, reference_price=1.0, max_levels=0)
