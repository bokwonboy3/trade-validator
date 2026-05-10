from __future__ import annotations

import pandas as pd
import pytest

from analysis.indicators import add_ma
from analysis.scanner_logic import (
    DEFAULT_SL_BUFFER_PCT,
    SynthesizedSetup,
    determine_direction,
    synthesize_setup,
)


def _df_with_swing_lows(*, last_close: float, swing_low_at: int, swing_low_price: float):
    """Build a 1H df with a clear swing low (strictly < surrounding lows).

    All other lows are set above swing_low_price so find_swings detects it.
    """
    n = 60
    base_low = swing_low_price + 0.5  # strictly above the swing
    lows = [base_low] * n
    lows[swing_low_at] = swing_low_price
    base_high = max(last_close, base_low) + 5
    highs = [base_high] * n
    closes = [last_close] * n
    return pd.DataFrame(
        {
            "openTime": list(range(n)),
            "open": [last_close] * n,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1.0] * n,
            "closeTime": [i + 1 for i in range(n)],
        }
    )


def _df_with_swing_highs(*, last_close: float, swing_high_at: int, swing_high_price: float):
    n = 60
    base_high = swing_high_price - 0.5  # strictly below the swing
    highs = [base_high] * n
    highs[swing_high_at] = swing_high_price
    base_low = min(last_close, base_high) - 5
    lows = [base_low] * n
    closes = [last_close] * n
    return pd.DataFrame(
        {
            "openTime": list(range(n)),
            "open": [last_close] * n,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1.0] * n,
            "closeTime": [i + 1 for i in range(n)],
        }
    )


# --- determine_direction ---
def test_determine_direction_long():
    closes = list(range(1, 101))
    df = pd.DataFrame({"close": closes})
    df = add_ma(df, [25, 99])
    assert determine_direction(df) == "long"


def test_determine_direction_short():
    closes = list(range(100, 0, -1))
    df = pd.DataFrame({"close": closes})
    df = add_ma(df, [25, 99])
    assert determine_direction(df) == "short"


def test_determine_direction_flat_returns_none():
    closes = [100.0] * 100
    df = pd.DataFrame({"close": closes})
    df = add_ma(df, [25, 99])
    assert determine_direction(df) is None


def test_determine_direction_nan_ma_returns_none():
    df = pd.DataFrame({"close": [100.0, 101.0]})
    df = add_ma(df, [25, 99])
    assert determine_direction(df) is None


# --- synthesize_setup ---
def test_synthesize_long_with_qualifying_swing():
    # last close 100; swing low 98 at index 30 (1.96% < 2% max distance)
    df = _df_with_swing_lows(last_close=100.0, swing_low_at=30, swing_low_price=98.0)
    setup = synthesize_setup(df, "long", n_swing=2, default_rr=3.0)
    assert setup is not None
    assert setup.direction == "long"
    assert setup.entry == pytest.approx(100.0)
    assert setup.sl_swing_price == pytest.approx(98.0)
    # SL = 98 * (1 - 0.003) = 97.706
    assert setup.sl == pytest.approx(98.0 * (1 - DEFAULT_SL_BUFFER_PCT))
    # R:R = 3.0 by construction
    risk = setup.entry - setup.sl
    reward = setup.tp - setup.entry
    assert reward / risk == pytest.approx(3.0)


def test_synthesize_short_with_qualifying_swing():
    df = _df_with_swing_highs(last_close=100.0, swing_high_at=30, swing_high_price=102.0)
    setup = synthesize_setup(df, "short", n_swing=2, default_rr=3.0)
    assert setup is not None
    assert setup.direction == "short"
    assert setup.sl_swing_price == pytest.approx(102.0)
    assert setup.sl == pytest.approx(102.0 * (1 + DEFAULT_SL_BUFFER_PCT))
    risk = setup.sl - setup.entry
    reward = setup.entry - setup.tp
    assert reward / risk == pytest.approx(3.0)


def test_synthesize_skips_when_swing_too_far():
    # Swing low at 90 (10% below entry 100) — outside default 2% max distance
    df = _df_with_swing_lows(last_close=100.0, swing_low_at=30, swing_low_price=90.0)
    assert synthesize_setup(df, "long", n_swing=2) is None


def test_synthesize_picks_closest_swing_low_to_entry():
    # Two swing lows: 95 (idx 10) and 98 (idx 40). Both qualify (within 2% of 100).
    # Should pick 98 (closer to entry).
    n = 60
    base_low = 99.0  # surrounding lows
    lows = [base_low] * n
    lows[10] = 95.0
    lows[40] = 98.0
    df = pd.DataFrame(
        {
            "openTime": list(range(n)),
            "open": [100.0] * n,
            "high": [105.0] * n,
            "low": lows,
            "close": [100.0] * n,
            "volume": [1.0] * n,
            "closeTime": [i + 1 for i in range(n)],
        }
    )
    setup = synthesize_setup(df, "long", n_swing=2)
    assert setup is not None
    assert setup.sl_swing_price == pytest.approx(98.0)


def test_synthesize_returns_none_when_no_swings_at_all():
    n = 60
    df = pd.DataFrame(
        {
            "openTime": list(range(n)),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1.0] * n,
            "closeTime": [i + 1 for i in range(n)],
        }
    )
    assert synthesize_setup(df, "long") is None
    assert synthesize_setup(df, "short") is None


def test_synthesize_buffer_must_be_within_layer4_tolerance():
    df = _df_with_swing_lows(last_close=100.0, swing_low_at=30, swing_low_price=98.0)
    # 0.5% buffer == Layer 4 tolerance — must reject (would put SL at boundary)
    with pytest.raises(ValueError, match="LAYER4_SL_TOLERANCE"):
        synthesize_setup(df, "long", sl_buffer_pct=0.005)


def test_synthesize_returns_none_for_empty_df():
    df = pd.DataFrame({"openTime": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "closeTime": []})
    assert synthesize_setup(df, "long") is None
