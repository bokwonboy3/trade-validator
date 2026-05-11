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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

from agents.runner import run_agentic_analysis_with_specialists
from agents.specialist_cache import DEFAULT_CACHE_PATH, SpecialistCache
from agents.types import AgentVerdict, SpecialistOutput
from alert_state import DEFAULT_STATE_PATH, AlertState
from analysis.forming import FormingResult, detect_forming_rejection
from analysis.indicators import add_ma
from analysis.layers import SetupEvaluation, evaluate_setup
from analysis.scanner_logic import (
    SynthesizedSetup,
    determine_direction,
    synthesize_setup,
)
from data.binance import BinanceError, fetch_klines
from monitor import run_position_monitor
from output.formatter import ValidationReport, format_report
from output.notify import append_jsonl, build_channels, dispatch
from scanner_config import ScannerConfig, load_config


# Exit codes
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_ALL_SYMBOLS_FAILED = 3

# Alert tiers
Tier = Literal["confirmed", "forming"]


@dataclass
class _AgentInputs:
    """DataFrames a deferred agent call needs. Held on the ScanResult so
    `_dispatch_new_setups` can run agents without re-fetching from Binance."""

    df_4h: pd.DataFrame
    df_1h: pd.DataFrame
    df_15m: pd.DataFrame
    df_1m: pd.DataFrame


@dataclass
class ScanResult:
    """Per-symbol scan outcome. Mutually exclusive: error / skipped / completed."""

    symbol: str
    error: str | None = None
    skipped_reason: str | None = None
    setup: SynthesizedSetup | None = None
    evaluation: SetupEvaluation | None = None
    forming: FormingResult | None = None  # set when in-progress 15m shows rejection forming
    agent_verdict: AgentVerdict | None = None  # agentic tier output (None if disabled)
    agent_specialists: list[SpecialistOutput] | None = None  # per-specialist outputs
    agent_cache_hit: bool = False  # True when verdict came from SpecialistCache
    agent_inputs: _AgentInputs | None = None  # frames for deferred agent call

    @property
    def passes(self) -> bool:
        return self.evaluation is not None and self.evaluation.passes

    @property
    def has_forming_signal(self) -> bool:
        """A FORMING signal triggers when the other 4 layers pass (Layers 1,2,4,5)
        but Layer 3 hasn't confirmed yet (fail/pending), AND the in-progress 15m
        window shows a rejection pattern forming."""
        if self.evaluation is None or self.forming is None:
            return False
        ev = self.evaluation
        # Layer 3 must NOT yet be confirmed pass — that's CONFIRMED, not FORMING
        if ev.layer_3.status == "pass":
            return False
        # Other 4 layers must be passes (otherwise the setup isn't viable anyway)
        return all(
            l.status == "pass" for l in (ev.layer_1, ev.layer_2, ev.layer_4, ev.layer_5)
        )


def scan_symbol(symbol: str, *, default_rr: float = 3.0) -> ScanResult:
    """Fetch + analyze one symbol. Returns a ScanResult; does not raise on
    BinanceError — packages it into the result so the loop continues.

    Fetches 4h/1h/15m for the 5-Layer evaluation, then 1m (with drop_unclosed=False)
    to feed the FORMING detector for the in-progress 15m candle.
    """
    try:
        df_4h = fetch_klines(symbol, "4h", limit=100)
        df_1h = fetch_klines(symbol, "1h", limit=100)
        df_15m = fetch_klines(symbol, "15m", limit=200)
        df_1m = fetch_klines(symbol, "1m", limit=30, drop_unclosed=False)
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

    # FORMING tier — only relevant when Layer 3 not yet confirmed
    forming: FormingResult | None = None
    if evaluation.layer_3.status != "pass":
        forming = detect_forming_rejection(
            df_1m, df_15m, entry=setup.entry, direction=direction,
            now_ms=int(time.time() * 1000),
        )

    # Agent invocation is deferred to `_dispatch_new_setups` so we only
    # spend on LLM calls for setups that pass the idempotency gate. Keep
    # the frames here so the dispatch path doesn't need to re-fetch.
    return ScanResult(
        symbol=symbol, setup=setup, evaluation=evaluation,
        forming=forming,
        agent_inputs=_AgentInputs(df_4h=df_4h, df_1h=df_1h, df_15m=df_15m, df_1m=df_1m),
    )


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
    if r.evaluation.total_score >= threshold:
        icon = "🟢"  # CONFIRMED
    elif r.has_forming_signal:
        icon = "⚡"  # FORMING — Layer 3 forming, others pass
    else:
        icon = "·"
    line = (
        f"{icon} {r.symbol} {r.setup.direction.upper()} "
        f"{r.evaluation.total_score}/5 "
        f"entry={r.setup.entry:,.2f} "
        f"SL={r.setup.sl:,.2f} TP={r.setup.tp:,.2f}"
    )
    if r.has_forming_signal:
        line += f"  [FORMING: {r.forming.minutes_elapsed}min/{r.forming.minutes_remaining}left]"
    return line


def build_report_for_alert(
    r: ScanResult, *, tier: Tier = "confirmed", alert_id: str | None = None,
) -> ValidationReport:
    """Wrap a ScanResult into a ValidationReport for the formatter.

    `tier="forming"` injects a header noting the signal is intra-candle.
    `alert_id` makes the report cross-referenceable in journal CLI.
    """
    assert r.setup is not None and r.evaluation is not None
    ev = r.evaluation
    symbol_label = r.symbol
    if tier == "forming" and r.forming is not None:
        symbol_label = (
            f"{r.symbol}  ⚡FORMING ({r.forming.minutes_elapsed}min in, "
            f"{r.forming.minutes_remaining}min until 15m close — re-verify on close)"
        )
    return ValidationReport(
        symbol=symbol_label,
        direction=r.setup.direction,
        entry=r.setup.entry,
        sl=r.setup.sl,
        tp=r.setup.tp,
        layer_1=ev.layer_1,
        layer_2=ev.layer_2,
        layer_3=ev.layer_3,
        layer_4=ev.layer_4,
        layer_5=ev.layer_5,
        advisory_15m=None,
        agent_verdict=r.agent_verdict,
        agent_specialists=r.agent_specialists,
        alert_id=alert_id,
    )


def _log(msg: str, *, quiet: bool, file=sys.stdout) -> None:
    """Print msg unless quiet=True. Errors should always go to stderr separately."""
    if not quiet:
        print(msg, file=file)


def _generate_alert_id(symbol: str, tier: str) -> str:
    """Stable short ID for cross-referencing: BTC-202605110430-confirmed.
    Same setup at same minute = same ID (idempotent under retry)."""
    from datetime import datetime, timezone
    short_symbol = symbol.replace("USDT", "").replace("BUSD", "")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"{short_symbol}-{ts}-{tier}"


def _record_jsonl(r: ScanResult, *, tier: str, alert_id: str) -> None:
    """Append a structured record per dispatched alert."""
    if r.setup is None or r.evaluation is None:
        return
    av = r.agent_verdict
    record = {
        "alert_id": alert_id,
        "symbol": r.symbol,
        "tier": tier,
        "direction": r.setup.direction,
        "entry": r.setup.entry,
        "sl": r.setup.sl,
        "tp": r.setup.tp,
        "tier1_score": r.evaluation.total_score,
        "layer_status": {
            "L1": r.evaluation.layer_1.status,
            "L2": r.evaluation.layer_2.status,
            "L3": r.evaluation.layer_3.status,
            "L4": r.evaluation.layer_4.status,
            "L5": r.evaluation.layer_5.status,
        },
        "agent_verdict": getattr(av, "verdict", None) if av else None,
        "agent_confidence": getattr(av, "confidence", None) if av else None,
        "agent_downgraded": getattr(av, "downgraded_from_tier1", False) if av else False,
        "agent_cache_hit": r.agent_cache_hit,
        "specialist_failures": [
            s.name for s in (r.agent_specialists or []) if getattr(s, "failed", False)
        ],
    }
    try:
        append_jsonl(record)
    except Exception as e:
        print(f"[scan] jsonl append failed: {e}", file=sys.stderr)


def run_scan(
    cfg: ScannerConfig,
    *,
    state: AlertState | None = None,
    agent_cache: SpecialistCache | None = None,
    quiet: bool = False,
) -> int:
    """Top-level scan + dispatch. Returns process exit code.

    If `state` is provided, deduplicates alerts within ALERT_TTL_HOURS so the
    same setup isn't re-sent on every scan tick.

    `agent_cache`, if provided, lets `_dispatch_new_setups` reuse specialist
    outputs for setups whose (symbol, direction, sl_swing_price) already
    appears within TTL. Missing → loaded from DEFAULT_CACHE_PATH.

    `quiet=True` suppresses per-symbol summary lines (cron-friendly: only
    dispatched alerts and errors reach stdout/stderr).
    """
    if state is None:
        state = AlertState.load()
    if agent_cache is None:
        agent_cache = SpecialistCache.load()
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

    channels = build_channels(cfg.notifications)

    confirmed = [
        r for r in results
        if r.passes and r.evaluation.total_score >= cfg.thresholds.min_score
    ]
    forming = [
        r for r in results
        if r not in confirmed and r.has_forming_signal
    ]

    if not confirmed and not forming:
        _log("\nNo setups crossed threshold.", quiet=quiet)
    else:
        _dispatch_new_setups(
            state, confirmed, forming, channels,
            agent_cache=agent_cache, quiet=quiet,
        )

    state.save()
    agent_cache.save()

    # Phase 5: monitor open trades regardless of new-setup outcome.
    # SL/TP touch → auto-close; 4H trend reversal → alert with inline buttons.
    _run_position_monitor_step(channels, quiet=quiet)

    return EXIT_OK


def _dispatch_new_setups(
    state: AlertState,
    confirmed: list[ScanResult],
    forming: list[ScanResult],
    channels: list,
    *,
    agent_cache: SpecialistCache,
    quiet: bool,
) -> None:
    """Idempotency check + agent invocation + dispatch.

    Agent calls happen here (not in `scan_symbol`) so they fire only for
    setups that survive `state.already_alerted` — avoiding ~5 LLM calls per
    re-detected setup on every cron tick. `agent_cache` further reuses
    specialist outputs within TTL on (symbol, direction, sl_swing_price).

    Tier prefix in the signature so a FORMING alert doesn't suppress a later
    CONFIRMED alert on the same setup."""
    def _record_key(symbol: str, tier: str) -> str:
        return f"{tier}:{symbol}"

    new_confirmed, suppressed_c = [], []
    for r in confirmed:
        sig = f"confirmed:{r.setup.direction}"
        if state.already_alerted(_record_key(r.symbol, "confirmed"), sig, r.setup.sl_swing_price):
            suppressed_c.append(r.symbol)
        else:
            new_confirmed.append(r)

    new_forming, suppressed_f = [], []
    for r in forming:
        sig = f"forming:{r.setup.direction}"
        if state.already_alerted(_record_key(r.symbol, "forming"), sig, r.setup.sl_swing_price):
            suppressed_f.append(r.symbol)
        else:
            new_forming.append(r)

    if suppressed_c or suppressed_f:
        _log(
            f"\nsuppressed (already alerted within 24h) — "
            f"confirmed:{suppressed_c} forming:{suppressed_f}",
            quiet=quiet,
        )

    if not new_confirmed and not new_forming:
        _log("No new setups to dispatch.", quiet=quiet)
        return

    _log(
        f"\ndispatching {len(new_confirmed)} CONFIRMED + {len(new_forming)} FORMING "
        f"→ {[c.name for c in channels]}",
        quiet=quiet,
    )
    for r in new_confirmed:
        assert r.setup is not None
        _attach_agent_verdict(r, agent_cache=agent_cache, quiet=quiet)
        alert_id = _generate_alert_id(r.symbol, "confirmed")
        report = build_report_for_alert(r, tier="confirmed", alert_id=alert_id)
        dispatch(channels, format_report(report), inline_keyboard=_alert_buttons(alert_id))
        state.record(
            _record_key(r.symbol, "confirmed"),
            f"confirmed:{r.setup.direction}",
            r.setup.sl_swing_price,
        )
        _record_jsonl(r, tier="confirmed", alert_id=alert_id)
    for r in new_forming:
        assert r.setup is not None
        _attach_agent_verdict(r, agent_cache=agent_cache, quiet=quiet)
        alert_id = _generate_alert_id(r.symbol, "forming")
        report = build_report_for_alert(r, tier="forming", alert_id=alert_id)
        dispatch(channels, format_report(report), inline_keyboard=_alert_buttons(alert_id))
        state.record(
            _record_key(r.symbol, "forming"),
            f"forming:{r.setup.direction}",
            r.setup.sl_swing_price,
        )
        _record_jsonl(r, tier="forming", alert_id=alert_id)


def _attach_agent_verdict(
    r: ScanResult, *, agent_cache: SpecialistCache, quiet: bool,
) -> None:
    """Populate `r.agent_verdict` / `r.agent_specialists` / `r.agent_cache_hit`
    by checking the cache first and falling back to a live LLM call.

    Mutates `r` in place. Agent failures are logged to stderr but do not
    raise — the alert still goes out without the agentic block.
    """
    assert r.setup is not None and r.evaluation is not None
    l3 = r.evaluation.layer_3.status
    cached = agent_cache.get(r.symbol, r.setup.direction, r.setup.sl_swing_price, l3)
    if cached is not None:
        r.agent_verdict, r.agent_specialists = cached
        r.agent_cache_hit = True
        _log(
            f"[agentic] {r.symbol} cache hit ({r.setup.direction} "
            f"@ swing {r.setup.sl_swing_price:.4f}, L3={l3})",
            quiet=quiet,
            file=sys.stderr,
        )
        return

    if r.agent_inputs is None:
        # scan_symbol always sets agent_inputs on a completed result; this
        # only fires for hand-built ScanResults in tests.
        return

    try:
        result = run_agentic_analysis_with_specialists(
            r.evaluation,
            df_15m=r.agent_inputs.df_15m, df_1m=r.agent_inputs.df_1m,
            df_4h=r.agent_inputs.df_4h, df_1h=r.agent_inputs.df_1h,
            symbol=r.symbol,
            entry=r.setup.entry, sl=r.setup.sl, tp=r.setup.tp,
            direction=r.setup.direction,
        )
    except Exception as e:
        # NEVER let agent failures block alert dispatch.
        print(f"[agentic] {r.symbol} analysis failed: {e}", file=sys.stderr)
        return

    if result is None:
        return
    r.agent_verdict, r.agent_specialists = result
    agent_cache.put(
        r.symbol, r.setup.direction, r.setup.sl_swing_price, l3,
        r.agent_verdict, r.agent_specialists,
    )


def _run_position_monitor_step(channels: list, *, quiet: bool) -> None:
    """Wrap monitor.run_position_monitor with dispatchers tied to the same
    channels scan uses. Isolated try/except so monitor failures don't take
    down the cron tick."""
    def text_dispatch(msg: str) -> None:
        dispatch(channels, msg)

    def button_dispatch(msg: str, inline_keyboard: list[list[dict]]) -> None:
        dispatch(channels, msg, inline_keyboard=inline_keyboard)

    try:
        events = run_position_monitor(
            dispatcher=text_dispatch,
            dispatcher_with_buttons=button_dispatch,
        )
        if events:
            summary = ", ".join(f"{e.kind}#{e.trade_id}" for e in events)
            _log(f"\n[monitor] {len(events)} event(s): {summary}", quiet=quiet)
        else:
            _log("\n[monitor] no events", quiet=quiet)
    except Exception as e:
        print(f"[monitor] error: {type(e).__name__}: {e}", file=sys.stderr)


def _alert_buttons(alert_id: str) -> list[list[dict]]:
    """Build inline-keyboard for Telegram so user can take/skip from phone.

    Three buttons; callback_data is what telegram_listener.py parses.
    """
    return [
        [
            {"text": "📥 진입 (시장가)", "callback_data": f"take_market:{alert_id}"},
        ],
        [
            {"text": "✏️ 진입 (가격 입력)", "callback_data": f"take_custom:{alert_id}"},
            {"text": "⏭ 패스", "callback_data": f"skip:{alert_id}"},
        ],
    ]


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
        "--agent-cache",
        default=str(DEFAULT_CACHE_PATH),
        help=f"specialist 결과 캐시 JSON 경로 (기본: {DEFAULT_CACHE_PATH})",
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
    p.add_argument(
        "--stats",
        action="store_true",
        help="최근 알람 통계 출력 후 종료 (scan 실행 안 함)",
    )
    p.add_argument(
        "--stats-hours",
        type=int,
        default=24,
        help="--stats lookback 시간 (default: 24)",
    )
    return p.parse_args(argv)


def print_stats(hours: int = 24, jsonl_path: str = "alerts.jsonl") -> None:
    """Read alerts.jsonl, print summary stats over last `hours` window."""
    import json
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    p = Path(jsonl_path)
    if not p.exists():
        print(f"No alerts logged yet ({jsonl_path} 없음).")
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                ts = datetime.fromisoformat(rec.get("ts", ""))
            except (json.JSONDecodeError, ValueError):
                continue
            if ts >= cutoff:
                recent.append(rec)

    if not recent:
        print(f"지난 {hours}h 동안 알람 없음.")
        return

    print(f"=== 최근 {hours}h 알람 통계 ===")
    print(f"총 알람: {len(recent)}건\n")

    by_symbol = Counter(r["symbol"] for r in recent)
    by_tier = Counter(r.get("tier", "?") for r in recent)
    by_agent_verdict = Counter(
        r.get("agent_verdict") for r in recent if r.get("agent_verdict")
    )
    downgrades = sum(1 for r in recent if r.get("agent_downgraded"))
    cache_hits = sum(1 for r in recent if r.get("agent_cache_hit"))
    failed_specs: Counter = Counter()
    for r in recent:
        for s in r.get("specialist_failures") or []:
            failed_specs[s] += 1

    print(f"심볼별: {dict(by_symbol)}")
    print(f"Tier별: {dict(by_tier)}")
    if by_agent_verdict:
        print(f"Agent verdict 분포: {dict(by_agent_verdict)}")
        print(f"Agent downgrade: {downgrades}/{len(recent)} ({100*downgrades//max(1,len(recent))}%)")
        print(f"Agent cache hit: {cache_hits}/{len(recent)} ({100*cache_hits//max(1,len(recent))}%)")
    if failed_specs:
        print(f"Specialist 실패 빈도: {dict(failed_specs)}")
    avg_conf = [r.get("agent_confidence") for r in recent if r.get("agent_confidence") is not None]
    if avg_conf:
        print(f"평균 confidence: {sum(avg_conf)/len(avg_conf):.0f}% (n={len(avg_conf)})")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.stats:
        print_stats(hours=args.stats_hours)
        return EXIT_OK
    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"❌ Config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    state = AlertState.load(Path(args.state))
    agent_cache = SpecialistCache.load(Path(args.agent_cache))
    return run_scan(cfg, state=state, agent_cache=agent_cache, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
