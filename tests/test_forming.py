from __future__ import annotations

import pandas as pd

from analysis.forming import (
    FIFTEEN_MIN_MS,
    FORMING_MIN_MINUTES,
    detect_forming_rejection,
)

ONE_MIN_MS = 60 * 1000


def _build_1m(rows: list[dict], window_open_ms: int) -> pd.DataFrame:
    """Build a 1m DataFrame from a list of dicts with default fields."""
    out = []
    for i, r in enumerate(rows):
        out.append(
            {
                "openTime": window_open_ms + i * ONE_MIN_MS,
                "open": r.get("open", 100.0),
                "high": r.get("high", 100.5),
                "low": r.get("low", 99.5),
                "close": r.get("close", 100.0),
                "volume": r.get("volume", 1.0),
                "closeTime": window_open_ms + (i + 1) * ONE_MIN_MS - 1,
            }
        )
    return pd.DataFrame(out)


def _df_15m_history(avg_volume: float = 100.0, n: int = 20) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "openTime": [i * FIFTEEN_MIN_MS for i in range(n)],
            "volume": [avg_volume] * n,
        }
    )


def test_detects_long_hammer_forming():
    """Aggregated 1m candles form a hammer with volume spike → FORMING signal."""
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned  # arbitrary aligned 15m boundary
    now_ms = window_open + 7 * ONE_MIN_MS  # 7 minutes into the window

    # 5 minutes of activity: price drops then recovers (hammer shape on aggregate)
    rows = [
        {"open": 100.0, "high": 100.1, "low": 96.0, "close": 96.5, "volume": 60},
        {"open": 96.5, "high": 97.0, "low": 96.0, "close": 96.8, "volume": 50},
        {"open": 96.8, "high": 100.0, "low": 96.5, "close": 99.5, "volume": 70},
        {"open": 99.5, "high": 100.2, "low": 99.0, "close": 100.0, "volume": 40},
        {"open": 100.0, "high": 100.5, "low": 99.8, "close": 100.4, "volume": 30},
    ]
    df_1m = _build_1m(rows, window_open)
    # Aggregate: open=100, close=100.4, body=0.4; low=96, lower_wick=4 (100→96);
    # high=100.5, upper_wick=0.1. Hammer: 4 > 2*0.4 ✓, 0.1 < 0.4 ✓
    # Total volume = 250. Baseline avg 15m = 100, threshold 150. 250 > 150 ✓

    df_15m = _df_15m_history(avg_volume=100.0)
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=now_ms)
    assert res is not None
    assert res.minutes_elapsed == 5
    assert res.virtual_low == 96.0
    assert res.virtual_high == 100.5
    assert res.virtual_volume == 250.0


def test_detects_short_shooting_star_forming():
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned
    now_ms = window_open + 6 * ONE_MIN_MS
    # Aggregate: open=100, close=99.6 (body 0.4 bearish);
    # high=104 (upper_wick=4 > 2*0.4 ✓), low=99.5 (lower_wick=0.1 < 0.4 ✓)
    rows = [
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 50},
        {"open": 100.2, "high": 104.0, "low": 100.1, "close": 103.5, "volume": 80},
        {"open": 103.5, "high": 103.8, "low": 100.0, "close": 100.5, "volume": 60},
        {"open": 100.5, "high": 100.5, "low": 99.6, "close": 99.6, "volume": 40},
    ]
    df_1m = _build_1m(rows, window_open)
    df_15m = _df_15m_history(avg_volume=100.0)
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="short", now_ms=now_ms)
    assert res is not None
    assert res.virtual_high == 104.0


def test_returns_none_when_volume_below_threshold():
    """망치형이지만 누적 거래량이 baseline × 1.5 미만이면 false."""
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned
    now_ms = window_open + 5 * ONE_MIN_MS
    rows = [
        {"open": 100.0, "high": 100.1, "low": 96.0, "close": 100.0, "volume": 30},
        {"open": 100.0, "high": 100.2, "low": 99.0, "close": 100.0, "volume": 30},
        {"open": 100.0, "high": 100.2, "low": 99.0, "close": 100.0, "volume": 30},
    ]
    df_1m = _build_1m(rows, window_open)  # total volume 90, baseline 100, threshold 150
    df_15m = _df_15m_history(avg_volume=100.0)
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=now_ms)
    assert res is None


def test_returns_none_when_not_in_zone():
    """가격 영역이 entry ±0.3% 밖이면 false."""
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned
    now_ms = window_open + 5 * ONE_MIN_MS
    rows = [
        {"open": 200.0, "high": 200.1, "low": 196.0, "close": 200.0, "volume": 100},
        {"open": 200.0, "high": 200.0, "low": 199.0, "close": 200.0, "volume": 100},
    ]
    df_1m = _build_1m(rows, window_open)
    df_15m = _df_15m_history(avg_volume=100.0)
    # Entry 100, virtual price range 196~200.1 — not overlapping with 100 ±0.3%
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=now_ms)
    assert res is None


def test_returns_none_when_not_a_rejection_pattern():
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned
    now_ms = window_open + 5 * ONE_MIN_MS
    # Aggregate: open 100, close 105 — big body up, no rejection
    rows = [
        {"open": 100.0, "high": 101.0, "low": 99.9, "close": 100.5, "volume": 60},
        {"open": 100.5, "high": 105.0, "low": 100.5, "close": 105.0, "volume": 100},
    ]
    df_1m = _build_1m(rows, window_open)
    df_15m = _df_15m_history(avg_volume=100.0)
    # Touch zone: entry 100, range 99.7~100.3 — 100 is in range
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=now_ms)
    assert res is None  # not a hammer


def test_returns_none_when_too_few_minutes_elapsed():
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned
    now_ms = window_open + 1 * ONE_MIN_MS  # only 1 minute in
    rows = [{"open": 100.0, "high": 100.1, "low": 96.0, "close": 100.0, "volume": 200}]
    df_1m = _build_1m(rows, window_open)
    df_15m = _df_15m_history(avg_volume=100.0)
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=now_ms)
    assert res is None  # less than FORMING_MIN_MINUTES (2)


def test_returns_none_when_empty_1m():
    df_1m = pd.DataFrame(
        {"openTime": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    )
    df_15m = _df_15m_history(avg_volume=100.0)
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=0)
    assert res is None


def test_minutes_remaining_property():
    window_open = 1_710_000_000_000  # divisible by 900_000 (15min) — aligned
    now_ms = window_open + 10 * ONE_MIN_MS
    rows = [
        {"open": 100.0, "high": 100.1, "low": 96.0, "close": 96.5, "volume": 100},
        {"open": 96.5, "high": 100.0, "low": 96.0, "close": 99.5, "volume": 100},
        {"open": 99.5, "high": 100.0, "low": 99.0, "close": 99.8, "volume": 50},
    ]
    df_1m = _build_1m(rows, window_open)
    df_15m = _df_15m_history(avg_volume=100.0)
    res = detect_forming_rejection(df_1m, df_15m, entry=100.0, direction="long", now_ms=now_ms)
    assert res is not None
    assert res.minutes_elapsed == 3
    # 15 - 3 = 12 minutes left in the window
    assert res.minutes_remaining == 12
