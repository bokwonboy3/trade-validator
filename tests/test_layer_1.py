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


# --- min_ma_gap_pct (Phase 2 tuning, 2026-05-11) ---
def test_weak_trend_below_min_gap_fails_both_directions():
    """When MA25 and MA99 are within 0.1% of each other, Layer 1 fails as 'weak_trend'.

    Mirrors the ETHUSDT case (Phase 1 iter 1 / iter 2): gap 0.005~0.007%
    produced LONG/SHORT flip — framework should reject as noise.
    """
    # Construct closes such that final MA25 ≈ MA99 with tiny gap (< 0.1%)
    # Constant 100s + tiny perturbation at the end so MA25 (window 25) slightly diverges.
    closes = [100.0] * 90 + [100.05] * 10  # tail-end nudges MA25 a bit above MA99
    df = add_ma(_df_from_closes(closes), [25, 99])
    res_long = layer_1_trend(df, "long")
    res_short = layer_1_trend(df, "short")
    assert res_long.status == "fail"
    assert res_short.status == "fail"
    assert res_long.detail["reason"] == "weak_trend"
    assert res_long.detail["ma_gap_pct"] < 0.001


def test_clear_trend_above_min_gap_still_passes():
    """A real trend (gap > 0.1%) is unaffected by the new filter."""
    closes = list(range(1, 101))  # ascending, MA25 well above MA99
    df = add_ma(_df_from_closes(closes), [25, 99])
    res = layer_1_trend(df, "long")
    assert res.status == "pass"
    # And the gap is in detail
    assert res.detail["ma_gap_pct"] > 0.001


def test_custom_min_gap_overrides_default():
    """Caller can supply a stricter min_ma_gap_pct (e.g., 1%) and reject borderline trends."""
    closes = list(range(1, 101))  # gap ~ a few % but not huge by the end
    df = add_ma(_df_from_closes(closes), [25, 99])
    # With min 0.5%, the ascending series may or may not pass; verify the filter mechanism works
    res_strict = layer_1_trend(df, "long", min_ma_gap_pct=0.5)  # 50% gap — almost everything fails
    assert res_strict.status == "fail"
    assert res_strict.detail["reason"] == "weak_trend"
