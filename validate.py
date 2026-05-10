"""Trade Validator CLI — 5-Layer setup evaluation for BTC perp futures."""
from __future__ import annotations

import argparse
import sys

from analysis.candles import Candle
from analysis.indicators import add_ma
from analysis.layers import (
    InputError,
    layer_1_trend,
    layer_2_setup_zone,
    layer_3_rejection,
    layer_4_sl_structure,
    layer_5_risk_reward,
    validate_inputs,
)
from data.binance import BinanceError, fetch_klines
from output.formatter import ValidationReport, format_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="validate.py",
        description="BTC perp 셋업을 5-Layer 프레임워크로 평가합니다.",
    )
    p.add_argument("--symbol", required=True, help="예: BTCUSDT")
    p.add_argument("--entry", required=True, type=float, help="진입가")
    p.add_argument("--sl", required=True, type=float, help="Stop Loss")
    p.add_argument("--tp", required=True, type=float, help="Take Profit")
    p.add_argument(
        "--direction",
        required=True,
        choices=["long", "short", "LONG", "SHORT"],
        help="long 또는 short",
    )
    p.add_argument(
        "--no-emoji",
        action="store_true",
        help="이모지 대신 [PASS]/[FAIL]/[PEND] 등 ASCII 라벨 사용",
    )
    return p.parse_args(argv)


def _fetch_advisory_15m(symbol: str) -> Candle | None:
    """Fetch the in-progress 15m candle (drop_unclosed=False) and return as a dict.

    Returns None if no live candle is detected (rare; happens right at boundary).
    """
    df = fetch_klines(symbol, "15m", limit=2, drop_unclosed=False)
    if len(df) == 0:
        return None
    last = df.iloc[-1]
    import time as _time
    now_ms = int(_time.time() * 1000)
    if int(last["closeTime"]) < now_ms:
        return None  # Last candle is already closed
    return {
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "close": float(last["close"]),
        "volume": float(last["volume"]),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    direction = args.direction.lower()
    symbol = args.symbol.upper()

    try:
        validate_inputs(args.entry, args.sl, args.tp, direction)
    except InputError as e:
        prefix = "[FAIL]" if args.no_emoji else "❌"
        print(f"{prefix} Input error: {e}", file=sys.stderr)
        return 2

    try:
        df_4h = fetch_klines(symbol, "4h", limit=100)
        df_1h = fetch_klines(symbol, "1h", limit=100)
        df_15m = fetch_klines(symbol, "15m", limit=200)
    except BinanceError as e:
        prefix = "[FAIL]" if args.no_emoji else "❌"
        print(f"{prefix} Binance error: {e}", file=sys.stderr)
        return 3

    df_4h_ma = add_ma(df_4h, [25, 99])
    df_1h_ma = add_ma(df_1h, [25, 99])

    l1 = layer_1_trend(df_4h_ma, direction)
    l2 = layer_2_setup_zone(df_1h_ma, args.entry)
    l3 = layer_3_rejection(df_1h, df_15m, args.entry, direction)
    l4 = layer_4_sl_structure(df_1h, args.sl, direction)
    l5 = layer_5_risk_reward(args.entry, args.sl, args.tp, direction)

    advisory = None
    try:
        advisory = _fetch_advisory_15m(symbol)
    except BinanceError:
        pass  # advisory is optional; ignore if it fails

    report = ValidationReport(
        symbol=symbol,
        direction=direction,
        entry=args.entry,
        sl=args.sl,
        tp=args.tp,
        layer_1=l1,
        layer_2=l2,
        layer_3=l3,
        layer_4=l4,
        layer_5=l5,
        advisory_15m=advisory,
    )
    print(format_report(report, no_emoji=args.no_emoji))
    return 0


if __name__ == "__main__":
    sys.exit(main())
