import pandas as pd

from analysis.layers import ONE_HOUR_MS, layer_3_rejection

FIFTEEN_MIN_MS = 15 * 60 * 1000


def _build_dfs(
    *,
    touch_low: float,
    touch_high: float,
    rejection_15m_idx: int | None,
    rejection_kind: str = "hammer",
    rejection_volume: float = 200.0,
    base_volume: float = 100.0,
):
    """Build minimal 1H + 15m frames with one touch candle and 200 15m candles.

    The "touch" 1H candle is the LAST 1H candle. Other 1H candles are far from
    the entry zone so they don't accidentally count as touches.
    """
    # 50 1H candles. Last one has the touch zone overlap; others are at price 200 (far).
    one_h_rows = []
    base_open = 1_700_000_000_000  # 2023-11-15 ish in UTC ms
    for i in range(50):
        open_time = base_open + i * ONE_HOUR_MS
        if i == 49:
            # The touch 1H candle
            row = {
                "openTime": open_time,
                "open": (touch_low + touch_high) / 2,
                "high": touch_high,
                "low": touch_low,
                "close": (touch_low + touch_high) / 2,
                "volume": 100.0,
                "closeTime": open_time + ONE_HOUR_MS - 1,
            }
        else:
            row = {
                "openTime": open_time,
                "open": 200.0,
                "high": 201.0,
                "low": 199.0,
                "close": 200.0,
                "volume": 100.0,
                "closeTime": open_time + ONE_HOUR_MS - 1,
            }
        one_h_rows.append(row)
    df_1h = pd.DataFrame(one_h_rows)

    # 200 15m candles ending at the same time the last 1H ends (50h window)
    fifteen_rows = []
    fifteen_base_open = base_open + 50 * ONE_HOUR_MS - 200 * FIFTEEN_MIN_MS
    for i in range(200):
        open_time = fifteen_base_open + i * FIFTEEN_MIN_MS
        # Default boring candle
        row = {
            "openTime": open_time,
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": base_volume,
            "closeTime": open_time + FIFTEEN_MIN_MS - 1,
        }
        fifteen_rows.append(row)

    if rejection_15m_idx is not None:
        # Replace the rejection candle. By default place inside the touch 1H window
        # (last 4 of 200 indices: 196..199).
        body_open = 100.0
        body_close = 100.5  # body 0.5
        if rejection_kind == "hammer":
            row = {
                **fifteen_rows[rejection_15m_idx],
                "open": body_open,
                "close": body_close,
                "high": 100.6,  # upper_wick 0.1 < body 0.5
                "low": 96.5,  # lower_wick 3.5 > 2*body
                "volume": rejection_volume,
            }
        elif rejection_kind == "shooting_star":
            body_open2 = 100.5
            body_close2 = 100.0
            row = {
                **fifteen_rows[rejection_15m_idx],
                "open": body_open2,
                "close": body_close2,
                "high": 104.5,  # upper_wick 4 > 2*body
                "low": 99.9,  # lower_wick 0.1 < body
                "volume": rejection_volume,
            }
        else:
            raise ValueError(rejection_kind)
        fifteen_rows[rejection_15m_idx] = row
    df_15m = pd.DataFrame(fifteen_rows)
    return df_1h, df_15m


def test_layer3_passes_when_hammer_with_volume_in_touch_window():
    # Entry 100, touch zone 99.7~100.3, last 1H low=99.8 high=100.2 → touch
    df_1h, df_15m = _build_dfs(
        touch_low=99.8, touch_high=100.2, rejection_15m_idx=198, rejection_kind="hammer"
    )
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "pass"
    assert res.score == 1


def test_layer3_pending_when_no_touch():
    # Last 1H is at 200, entry 100 → no touch
    df_1h, df_15m = _build_dfs(
        touch_low=200.0, touch_high=201.0, rejection_15m_idx=None
    )
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "pending"
    assert res.score == 0
    assert res.detail["reason"] == "no_touch_in_history"


def test_layer3_fails_when_touch_but_no_rejection():
    # Touch zone OK, but no rejection candle in window
    df_1h, df_15m = _build_dfs(
        touch_low=99.8, touch_high=100.2, rejection_15m_idx=None
    )
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "fail"
    assert res.score == 0


def test_layer3_fails_when_rejection_but_low_volume():
    # Hammer present but volume = base (no spike)
    df_1h, df_15m = _build_dfs(
        touch_low=99.8,
        touch_high=100.2,
        rejection_15m_idx=198,
        rejection_kind="hammer",
        rejection_volume=100.0,  # equal to base, no spike
    )
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "fail"


def test_layer3_short_uses_shooting_star():
    df_1h, df_15m = _build_dfs(
        touch_low=99.8,
        touch_high=100.2,
        rejection_15m_idx=198,
        rejection_kind="shooting_star",
    )
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="short")
    assert res.status == "pass"


def test_layer3_long_does_not_match_shooting_star():
    df_1h, df_15m = _build_dfs(
        touch_low=99.8,
        touch_high=100.2,
        rejection_15m_idx=198,
        rejection_kind="shooting_star",
    )
    # LONG looking for hammer, but only shooting star present → fail
    res = layer_3_rejection(df_1h, df_15m, entry=100.0, direction="long")
    assert res.status == "fail"
