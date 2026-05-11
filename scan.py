"""Multi-symbol scanner — runs the 5-Layer framework across configured symbols.

For each symbol in config.toml, the scanner:
1. Fetches 4h/1h/15m candles from Binance
2. Picks direction from Layer 1 (4H trend) — skips on flat market
3. Synthesizes a candidate setup (entry from current price, SL from nearest swing,
   TP for default_rr R:R) — skips if no clean swing within reach
4. Runs evaluate_setup() on the synthetic setup
5. Prints a one-line summary for every symbol; dispatches a full report through
   the configured notification channels for setups with score >= min_score.

Designed for one-shot invocation (cron-friendly). Repeat scheduling is the
responsibility of cron/systemd — running this script in a tight Python loop
doesn't add anything.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from alert_state import DEFAULT_STATE_PATH, AlertState
from analysis.indicators import add_ma
from analysis.layers import SetupEvaluation, evaluate_setup
from analysis.scanner_logic import (
    SynthesizedSetup,
    determine_direction,
    synthesize_setup,
)
from data.binance import BinanceError, fetch_klines
from output.formatter import ValidationReport, format_report
from output.notify import build_channels, dispatch
from scanner_config import ScannerConfig, load_config


# Exit codes
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_ALL_SYMBOLS_FAILED = 3


@dataclass
class ScanResult:
    """Per-symbol scan outcome. Mutually exclusive: error / skipped / completed."""

    symbol: str
    error: str | None = None
    skipped_reason: str | None = None
    setup: SynthesizedSetup | None = None
    evaluation: SetupEvaluation | None = None

    @property
    def passes(self) -> bool:
        return self.evaluation is not None and self.evaluation.passes


def scan_symbol(symbol: str, *, default_rr: float = 3.0) -> ScanResult:
    """Fetch + analyze one symbol. Returns a ScanResult; does not raise on
    BinanceError — packages it into the result so the loop continues."""
    try:
        df_4h = fetch_klines(symbol, "4h", limit=100)
        df_1h = fetch_klines(symbol, "1h", limit=100)
        df_15m = fetch_klines(symbol, "15m", limit=200)
    except BinanceError as e:
        return ScanResult(symbol=symbol, error=str(e))

    df_4h_ma = add_ma(df_4h, [25, 99])
    direction = determine_direction(df_4h_ma)
    if direction is None:
        return ScanResult(symbol=symbol, skipped_reason="no clear 4H trend (MA25 ≈ MA99)")

    setup = synthesize_setup(df_1h, direction, default_rr=default_rr)
    if setup is None:
        return ScanResult(
            symbol=symbol,
            skipped_reason=f"no qualifying swing within reach for {direction}",
        )

    evaluation = evaluate_setup(
        df_4h, df_1h, df_15m,
        entry=setup.entry, sl=setup.sl, tp=setup.tp, direction=direction,
    )
    return ScanResult(symbol=symbol, setup=setup, evaluation=evaluation)


def scan(cfg: ScannerConfig) -> list[ScanResult]:
    """Scan all configured symbols sequentially."""
    return [scan_symbol(s, default_rr=cfg.thresholds.default_rr) for s in cfg.symbols]


def format_summary_line(r: ScanResult, threshold: int) -> str:
    """One-line per symbol for the stdout overview."""
    if r.error:
        return f"❌ {r.symbol}: {r.error}"
    if r.skipped_reason:
        return f"⏭ {r.symbol}: skipped — {r.skipped_reason}"
    assert r.setup is not None and r.evaluation is not None
    icon = "🟢" if r.evaluation.total_score >= threshold else "·"
    return (
        f"{icon} {r.symbol} {r.setup.direction.upper()} "
        f"{r.evaluation.total_score}/5 "
        f"entry={r.setup.entry:,.2f} "
        f"SL={r.setup.sl:,.2f} TP={r.setup.tp:,.2f}"
    )


def build_report_for_alert(r: ScanResult) -> ValidationReport:
    """Wrap a passing ScanResult into a ValidationReport for the formatter."""
    assert r.setup is not None and r.evaluation is not None
    ev = r.evaluation
    return ValidationReport(
        symbol=r.symbol,
        direction=r.setup.direction,
        entry=r.setup.entry,
        sl=r.setup.sl,
        tp=r.setup.tp,
        layer_1=ev.layer_1,
        layer_2=ev.layer_2,
        layer_3=ev.layer_3,
        layer_4=ev.layer_4,
        layer_5=ev.layer_5,
        advisory_15m=None,  # scanner mode skips advisory to keep alerts deterministic
    )


def _log(msg: str, *, quiet: bool, file=sys.stdout) -> None:
    """Print msg unless quiet=True. Errors should always go to stderr separately."""
    if not quiet:
        print(msg, file=file)


def run_scan(
    cfg: ScannerConfig,
    *,
    state: AlertState | None = None,
    quiet: bool = False,
) -> int:
    """Top-level scan + dispatch. Returns process exit code.

    If `state` is provided, deduplicates alerts within ALERT_TTL_HOURS so the
    same setup isn't re-sent on every scan tick.

    `quiet=True` suppresses per-symbol summary lines (cron-friendly: only
    dispatched alerts and errors reach stdout/stderr).
    """
    if state is None:
        state = AlertState.load()
    results = scan(cfg)

    if not quiet:
        print("=== Scan Summary ===")
        for r in results:
            print(format_summary_line(r, cfg.thresholds.min_score))

    # Detect total API failure: every symbol erred (network down, Binance outage, etc.)
    if results and all(r.error for r in results):
        for r in results:
            print(f"❌ {r.symbol}: {r.error}", file=sys.stderr)
        return EXIT_ALL_SYMBOLS_FAILED

    # Errors that occur for some (but not all) symbols still emit to stderr
    if quiet:
        for r in results:
            if r.error:
                print(f"❌ {r.symbol}: {r.error}", file=sys.stderr)

    passing = [
        r for r in results
        if r.passes and r.evaluation.total_score >= cfg.thresholds.min_score
    ]
    if not passing:
        _log("\nNo setups crossed threshold.", quiet=quiet)
        state.save()
        return EXIT_OK

    new_alerts = []
    suppressed = []
    for r in passing:
        assert r.setup is not None
        if state.already_alerted(r.symbol, r.setup.direction, r.setup.sl_swing_price):
            suppressed.append(r.symbol)
        else:
            new_alerts.append(r)

    if suppressed:
        _log(f"\n{len(suppressed)} suppressed (already alerted within 24h): {suppressed}",
             quiet=quiet)

    if not new_alerts:
        _log("No new setups to dispatch.", quiet=quiet)
        state.save()
        return EXIT_OK

    channels = build_channels(cfg.notifications)
    _log(f"\n{len(new_alerts)} new setup(s) — dispatching to {[c.name for c in channels]}",
         quiet=quiet)
    for r in new_alerts:
        assert r.setup is not None
        report = build_report_for_alert(r)
        text = format_report(report)
        dispatch(channels, text)
        state.record(r.symbol, r.setup.direction, r.setup.sl_swing_price)

    state.save()
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scan.py",
        description="Multi-symbol 5-Layer scanner. Designed to be run by cron.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="config.toml 경로 (기본: ./config.toml, 없으면 ./config.example.toml)",
    )
    p.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"alert state JSON 경로 (기본: {DEFAULT_STATE_PATH})",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="명시적 one-shot 모드 (현재 default와 동일; 미래 호환용 플래그)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Cron 모드용 — 셋업이 통과해서 dispatch될 때만 stdout 출력",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"❌ Config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    state = AlertState.load(Path(args.state))
    return run_scan(cfg, state=state, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
