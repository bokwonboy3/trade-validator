import pandas as pd

from analysis.indicators import add_ma
from analysis.layers import layer_1_trend


def _df_from_closes(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_long_passes_when_ma25_above_ma99():
    # 99 closes ascending → recent MA25 > MA99
    closes = list(range(1, 101))  # 1..100
    df = add_ma(_df_from_closes(closes), [25, 99])
    res = layer_1_trend(df, "long")
    assert res.status == "pass"
    assert res.score == 1
    assert res.detail["ma25"] > res.detail["ma99"]


def test_long_fails_when_ma25_below_ma99():
    closes = list(range(100, 0, -1))  # descending
    df = add_ma(_df_from_closes(closes), [25, 99])
    res = layer_1_trend(df, "long")
    assert res.status == "fail"
    assert res.score == 0


def test_short_passes_when_ma25_below_ma99():
    closes = list(range(100, 0, -1))
    df = add_ma(_df_from_closes(closes), [25, 99])
    res = layer_1_trend(df, "short")
    assert res.status == "pass"
    assert res.score == 1


def test_equal_mas_fail():
    closes = [100.0] * 100
    df = add_ma(_df_from_closes(closes), [25, 99])
    res_long = layer_1_trend(df, "long")
    res_short = layer_1_trend(df, "short")
    assert res_long.status == "fail"
    assert res_short.status == "fail"
