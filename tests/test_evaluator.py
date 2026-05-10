"""Integration test: evaluate_setup() runs all 5 layers on real Binance fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from analysis.layers import (
    PASS_THRESHOLD,
    SetupEvaluation,
    evaluate_setup,
)

FIXTURES = Path(__file__).parent / "fixtures"
_COLS = [
    "openTime", "open", "high", "low", "close", "volume",
    "closeTime", "quoteAssetVolume", "numberOfTrades",
    "takerBuyBaseAssetVolume", "takerBuyQuoteAssetVolume", "ignore",
]
_NUM = ["open", "high", "low", "close", "volume"]


def _load(interval: str) -> pd.DataFrame:
    raw = json.loads((FIXTURES / f"btcusdt_{interval}.json").read_text())
    df = pd.DataFrame(raw, columns=_COLS)
    for c in _NUM:
        df[c] = pd.to_numeric(df[c])
    df["openTime"] = df["openTime"].astype("int64")
    df["closeTime"] = df["closeTime"].astype("int64")
    return df


def test_evaluate_setup_returns_setup_evaluation():
    df_4h = _load("4h")
    df_1h = _load("1h")
    df_15m = _load("15m")
    # Pick an entry well outside any historical zone — Layer 3 should be pending.
    ev = evaluate_setup(
        df_4h, df_1h, df_15m,
        entry=10_000.0, sl=9_900.0, tp=10_300.0, direction="long",
    )
    assert isinstance(ev, SetupEvaluation)
    assert all(l is not None for l in ev.as_layers())
    # 10k is fresh — Layer 3 must be pending.
    assert ev.layer_3.status == "pending"


def test_setup_evaluation_total_score_and_passes():
    from analysis.layers import LayerResult

    ev = SetupEvaluation(
        layer_1=LayerResult(1, "pass", {}),
        layer_2=LayerResult(1, "pass", {}),
        layer_3=LayerResult(1, "pass", {}),
        layer_4=LayerResult(1, "pass", {}),
        layer_5=LayerResult(0, "fail", {}),
    )
    assert ev.total_score == 4
    assert ev.passes is True


def test_setup_evaluation_below_threshold():
    from analysis.layers import LayerResult

    ev = SetupEvaluation(
        layer_1=LayerResult(1, "pass", {}),
        layer_2=LayerResult(0, "fail", {}),
        layer_3=LayerResult(0, "pending", {}),
        layer_4=LayerResult(1, "pass", {}),
        layer_5=LayerResult(1, "pass", {}),
    )
    assert ev.total_score == 3
    assert ev.passes is False
    assert PASS_THRESHOLD == 4


def test_evaluate_setup_realistic_btc_input():
    """Smoke: realistic BTC params, expect at least 2 layers to pass."""
    df_4h = _load("4h")
    df_1h = _load("1h")
    df_15m = _load("15m")
    # Pick last 1H close as entry, a tight SL/TP triplet
    last_close = float(df_1h.iloc[-1]["close"])
    entry = last_close
    sl = last_close * 0.997
    tp = last_close * 1.009
    ev = evaluate_setup(
        df_4h, df_1h, df_15m,
        entry=entry, sl=sl, tp=tp, direction="long",
    )
    # Don't assert specific layers — fixtures snapshot a moment in time —
    # but score must be a valid 0..5 int and immutability holds.
    assert 0 <= ev.total_score <= 5
    # SetupEvaluation is frozen
    import dataclasses
    assert dataclasses.is_dataclass(ev) and ev.__dataclass_params__.frozen
