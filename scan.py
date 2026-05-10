"""Multi-symbol scanner — runs the 5-Layer framework across configured symbols.

For each symbol in config.toml, the scanner:
1. Fetches 4h/1h/15m candles from Binance
2. Picks direction from Layer 1 (4H trend) — skips on flat market
3. Synthesizes a candidate setup (entry from current price, SL from nearest swing,
   TP for default_rr R:R) — skips if no clean swing within reach
4. Runs evaluate_setup() on the synthetic setup
5. Prints a one-line summary for every symbol; dispatches a full report through
   the configured notification channels for setups with score >= min_score.

Idempotency (don't re-alert the same setup) is added in B4. CLI flags (--once,
--config) come in B7.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

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


def run_scan(cfg: ScannerConfig) -> int:
    """Top-level scan + dispatch. Returns process exit code."""
    results = scan(cfg)

    print("=== Scan Summary ===")
    for r in results:
        print(format_summary_line(r, cfg.thresholds.min_score))

    passing = [r for r in results if r.passes and r.evaluation.total_score >= cfg.thresholds.min_score]
    if not passing:
        print("\nNo setups crossed threshold.")
        return 0

    channels = build_channels(cfg.notifications)
    print(f"\n{len(passing)} setup(s) passed — dispatching to {[c.name for c in channels]}")
    for r in passing:
        report = build_report_for_alert(r)
        text = format_report(report)
        dispatch(channels, text)

    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print(f"❌ Config error: {e}", file=sys.stderr)
        return 2
    return run_scan(cfg)


if __name__ == "__main__":
    sys.exit(main())
