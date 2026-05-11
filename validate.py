"""Trade Validator CLI — 5-Layer setup evaluation for BTC perp futures."""
from __future__ import annotations

import argparse
import sys

from analysis.candles import Candle
from analysis.layers import (
    InputError,
    evaluate_setup,
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

    ev = evaluate_setup(
        df_4h, df_1h, df_15m,
        entry=args.entry, sl=args.sl, tp=args.tp, direction=direction,
    )

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
        layer_1=ev.layer_1,
        layer_2=ev.layer_2,
        layer_3=ev.layer_3,
        layer_4=ev.layer_4,
        layer_5=ev.layer_5,
        advisory_15m=advisory,
    )
    print(format_report(report, no_emoji=args.no_emoji))
    return 0


if __name__ == "__main__":
    sys.exit(main())
