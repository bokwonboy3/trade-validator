import pytest

from analysis.candles import is_hammer, is_rejection, is_shooting_star, is_volume_spike


def _candle(o, h, lo, c, v=1.0):
    return {"open": o, "high": h, "low": lo, "close": c, "volume": v}


# --- Hammer ---
def test_hammer_classic():
    # body 0.5 (10.0→10.5), lower_wick 4.0 (10.0→6.0), upper_wick 0 (10.5→10.5)
    assert is_hammer(_candle(o=10.0, h=10.5, lo=6.0, c=10.5))


def test_hammer_with_small_upper_wick():
    # body 1, lower_wick 3, upper_wick 0.5 → upper < body OK
    assert is_hammer(_candle(o=10.0, h=11.5, lo=7.0, c=11.0))


def test_not_hammer_when_upper_wick_too_long():
    # upper_wick = body → fails (must be strictly less)
    assert not is_hammer(_candle(o=10.0, h=12.0, lo=7.0, c=11.0))


def test_not_hammer_when_lower_wick_short():
    # lower_wick exactly 2×body → strictly greater required, fails
    assert not is_hammer(_candle(o=10.0, h=10.5, lo=8.0, c=11.0))


def test_doji_rejected_as_hammer():
    # body 0 → doji
    assert not is_hammer(_candle(o=10.0, h=11.0, lo=8.0, c=10.0))


# --- Shooting Star ---
def test_shooting_star_classic():
    # body 0.5 (10.5→10.0), upper_wick 4 (10.5→14.5), lower_wick 0
    assert is_shooting_star(_candle(o=10.5, h=14.5, lo=10.0, c=10.0))


def test_not_shooting_star_when_lower_wick_long():
    # lower_wick = body → not shooting star
    assert not is_shooting_star(_candle(o=10.5, h=14.0, lo=9.5, c=10.0))


# --- Volume spike ---
def test_volume_spike_passes():
    assert is_volume_spike(volume=160, prev_volumes=[100] * 10)  # 160 > 150


def test_volume_spike_fails_at_exact_multiple():
    # 150 > 100*1.5 → False (strictly greater required)
    assert not is_volume_spike(volume=150, prev_volumes=[100] * 10)


def test_volume_spike_no_history():
    assert not is_volume_spike(volume=200, prev_volumes=[])


def test_volume_spike_zero_avg():
    assert not is_volume_spike(volume=200, prev_volumes=[0] * 5)


# --- Direction-aware rejection ---
def test_rejection_long_uses_hammer():
    hammer = _candle(o=10.0, h=10.5, lo=6.0, c=10.5)
    assert is_rejection(hammer, "long")
    assert not is_rejection(hammer, "short")


def test_rejection_short_uses_shooting_star():
    star = _candle(o=10.5, h=14.5, lo=10.0, c=10.0)
    assert is_rejection(star, "short")
    assert not is_rejection(star, "long")


def test_rejection_unknown_direction():
    with pytest.raises(ValueError):
        is_rejection(_candle(10, 11, 9, 10), "sideways")
