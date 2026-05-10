import pandas as pd
import pytest

from analysis.layers import (
    InputError,
    layer_4_sl_structure,
    layer_5_risk_reward,
    validate_inputs,
)


def _df_with_swing_low(low_at_idx: int = 5, low_price: float = 95.0):
    """Build a 1H frame with a clear swing low at the given index."""
    n = 50
    highs = [101.0] * n
    lows = [100.0] * n
    lows[low_at_idx] = low_price
    return pd.DataFrame(
        {
            "openTime": [i * 3_600_000 for i in range(n)],
            "open": [100.0] * n,
            "high": highs,
            "low": lows,
            "close": [100.0] * n,
            "volume": [1.0] * n,
            "closeTime": [(i + 1) * 3_600_000 - 1 for i in range(n)],
        }
    )


def _df_with_swing_high(high_at_idx: int = 5, high_price: float = 105.0):
    n = 50
    highs = [101.0] * n
    lows = [100.0] * n
    highs[high_at_idx] = high_price
    return pd.DataFrame(
        {
            "openTime": [i * 3_600_000 for i in range(n)],
            "open": [100.0] * n,
            "high": highs,
            "low": lows,
            "close": [100.0] * n,
            "volume": [1.0] * n,
            "closeTime": [(i + 1) * 3_600_000 - 1 for i in range(n)],
        }
    )


# --- Layer 4 ---
def test_layer4_long_passes_when_sl_just_below_swing_low():
    df = _df_with_swing_low(low_price=95.0)
    res = layer_4_sl_structure(df, sl=94.8, direction="long", n_swing=2)
    assert res.status == "pass"


def test_layer4_long_fails_when_sl_above_swing_low():
    df = _df_with_swing_low(low_price=95.0)
    # SL above swing low → invalid (would hit before structure breaks)
    res = layer_4_sl_structure(df, sl=95.1, direction="long", n_swing=2)
    assert res.status == "fail"


def test_layer4_long_fails_when_sl_too_far_below():
    df = _df_with_swing_low(low_price=95.0)
    # 0.5% of 95 = 0.475 → SL below 94.525 fails
    res = layer_4_sl_structure(df, sl=94.0, direction="long", n_swing=2)
    assert res.status == "fail"


def test_layer4_short_passes_when_sl_just_above_swing_high():
    df = _df_with_swing_high(high_price=105.0)
    res = layer_4_sl_structure(df, sl=105.2, direction="short", n_swing=2)
    assert res.status == "pass"


def test_layer4_short_fails_when_sl_below_swing_high():
    df = _df_with_swing_high(high_price=105.0)
    res = layer_4_sl_structure(df, sl=104.9, direction="short", n_swing=2)
    assert res.status == "fail"


def test_layer4_no_swings_fails():
    n = 50
    df = pd.DataFrame(
        {
            "openTime": [i * 3_600_000 for i in range(n)],
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [100.0] * n,
            "close": [100.0] * n,
            "volume": [1.0] * n,
            "closeTime": [(i + 1) * 3_600_000 - 1 for i in range(n)],
        }
    )
    res = layer_4_sl_structure(df, sl=99.0, direction="long", n_swing=2)
    assert res.status == "fail"


# --- Input validation ---
def test_input_validation_long_sl_above_entry():
    with pytest.raises(InputError, match="LONG: SL"):
        validate_inputs(entry=100.0, sl=101.0, tp=110.0, direction="long")


def test_input_validation_long_tp_below_entry():
    with pytest.raises(InputError, match="LONG: TP"):
        validate_inputs(entry=100.0, sl=99.0, tp=99.0, direction="long")


def test_input_validation_short_sl_below_entry():
    with pytest.raises(InputError, match="SHORT: SL"):
        validate_inputs(entry=100.0, sl=99.0, tp=90.0, direction="short")


def test_input_validation_short_tp_above_entry():
    with pytest.raises(InputError, match="SHORT: TP"):
        validate_inputs(entry=100.0, sl=101.0, tp=110.0, direction="short")


def test_input_validation_passes_clean_long():
    validate_inputs(entry=100.0, sl=99.0, tp=103.5, direction="long")


# --- Layer 5 ---
def test_layer5_long_passes_at_rr_3():
    # risk 1, reward 3 → R:R = 3
    res = layer_5_risk_reward(entry=100.0, sl=99.0, tp=103.0, direction="long")
    assert res.status == "pass"
    assert res.detail["rr"] == pytest.approx(3.0)


def test_layer5_long_fails_below_rr_3():
    res = layer_5_risk_reward(entry=100.0, sl=99.0, tp=102.0, direction="long")
    assert res.status == "fail"
    assert res.detail["rr"] == pytest.approx(2.0)


def test_layer5_short_passes():
    # entry 100, sl 101, tp 97 → reward 3, risk 1, R:R 3
    res = layer_5_risk_reward(entry=100.0, sl=101.0, tp=97.0, direction="short")
    assert res.status == "pass"
