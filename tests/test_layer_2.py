import pandas as pd

from analysis.indicators import add_ma
from analysis.layers import layer_2_setup_zone


def _df(highs, lows):
    n = len(highs)
    closes = [(h + lo) / 2 for h, lo in zip(highs, lows)]
    return pd.DataFrame(
        {"high": highs, "low": lows, "close": closes, "open": closes, "volume": [1] * n}
    )


def _df_with_ma(highs, lows):
    return add_ma(_df(highs, lows), [25, 99])


def test_passes_when_entry_near_swing_high():
    # Build series with a clear swing high at index 5, value ~100
    highs = [80, 82, 85, 88, 92, 100, 92, 88, 85, 82, 80] + [80] * 90
    lows = [70] * len(highs)
    df = _df_with_ma(highs, lows)
    res = layer_2_setup_zone(df, entry=100.2, n_swing=2)  # 0.2% above swing
    assert res.status == "pass"
    assert res.detail["closest_label"].startswith("swing_high")


def test_fails_when_entry_far_from_levels():
    highs = [80, 82, 85, 88, 92, 100, 92, 88, 85, 82, 80] + [80] * 90
    lows = [70] * len(highs)
    df = _df_with_ma(highs, lows)
    # Entry at 50, all levels around 70-100 → far away
    res = layer_2_setup_zone(df, entry=50.0, n_swing=2)
    assert res.status == "fail"
    assert res.detail["distance_pct"] > 0.003


def test_ma_levels_included():
    # Constant price → MA25/MA99 ≈ 100. No swings. Entry near 100 should pass via MA.
    highs = [101] * 100
    lows = [99] * 100
    df = _df_with_ma(highs, lows)
    res = layer_2_setup_zone(df, entry=100.1, n_swing=5)
    assert res.status == "pass"
    assert res.detail["closest_label"] in ("ma25_1h", "ma99_1h")


def test_tolerance_boundary():
    highs = [80, 82, 85, 88, 92, 100, 92, 88, 85, 82, 80] + [80] * 90
    lows = [70] * len(highs)
    df = _df_with_ma(highs, lows)
    # Entry exactly at +0.3% of 100 = 100.3 → boundary, should pass (≤)
    res = layer_2_setup_zone(df, entry=100.3, n_swing=2)
    assert res.status == "pass"
    # Entry at +0.31% should fail
    res2 = layer_2_setup_zone(df, entry=100.31, n_swing=2)
    assert res2.status == "fail"
