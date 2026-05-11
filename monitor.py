"""Position monitor — sustains the trade after entry (Phase 5 + Phase 6).

For every open trade in journal.db, this module:
  1. checks the latest 1m candles for SL/TP touch → auto-closes + alerts (P5)
  2. checks 4H Layer 1 trend; if it reversed against the trade direction,
     sends a "추세 반전" alert with inline buttons (idempotent: alerts once
     per (trade_id, new-trend-direction) within TTL) (P5)
  3. computes live unrealized PnL for /status command (no side effects) (P5)
  4. detects PnL milestone crossings (±1%, ±2%, ±5%) → notify + buttons (P6)
  5. periodically runs position_advisor (LLM) to recommend HOLD/TIGHTEN/
     PARTIAL/EXIT every ADVISOR_INTERVAL_HOURS per trade (P6)

Idempotency state: `.tv-monitor-state.json` tracks per-trade:
  - alerted_reversals (trend reversal direction already alerted)
  - alerted_milestones (PnL thresholds already alerted)
  - last_advisor_at (timestamp of last advisor LLM call)

Designed to be called from scan.py at the end of a cron tick.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Protocol

import pandas as pd

from analysis.indicators import add_ma
from analysis.layers import layer_1_trend
from data.binance import BinanceError, fetch_klines
from data.journal_db import DEFAULT_DB_PATH, close_trade, connect, open_trades

DEFAULT_MONITOR_STATE_PATH: Final = Path(".tv-monitor-state.json")
REVERSAL_TTL_HOURS: Final[int] = 24
TOUCH_CHECK_KLINE_LIMIT: Final[int] = 5  # last 5 1m candles

# Phase 6: PnL milestone thresholds (in % — positive AND negative).
# Each fires once per trade lifetime (idempotent via state).
PNL_MILESTONES: Final[tuple[float, ...]] = (-5.0, -2.0, -1.0, 1.0, 2.0, 5.0)

# Phase 6: how often (per trade) to run the LLM position advisor.
ADVISOR_INTERVAL_HOURS: Final[float] = 4.0


# --- Monitor state (idempotency for trend reversal + milestone + advisor) ---
@dataclass
class ReversalAlertRecord:
    """One row per (trade_id, new-trend) we've already alerted on."""

    alerted_at: str  # ISO 8601, UTC
    reverse_to: str  # "long" or "short" or "neutral"


@dataclass
class MonitorState:
    """Persisted state for monitor idempotency.

    Schema:
    {
      "alerted_reversals": {"<trade_id>": {alerted_at, reverse_to}},
      "alerted_milestones": {"<trade_id>": [list of float pcts]},
      "last_advisor_at": {"<trade_id>": ISO timestamp}
    }
    """

    alerted_reversals: dict[str, ReversalAlertRecord] = field(default_factory=dict)
    alerted_milestones: dict[str, list[float]] = field(default_factory=dict)
    last_advisor_at: dict[str, str] = field(default_factory=dict)
    path: Path = field(default_factory=lambda: DEFAULT_MONITOR_STATE_PATH)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_MONITOR_STATE_PATH) -> "MonitorState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        raw = json.loads(p.read_text())
        cutoff = _now_utc() - timedelta(hours=REVERSAL_TTL_HOURS)
        fresh_reversals: dict[str, ReversalAlertRecord] = {}
        for tid, info in (raw.get("alerted_reversals") or {}).items():
            try:
                rec = ReversalAlertRecord(**info)
                if datetime.fromisoformat(rec.alerted_at) >= cutoff:
                    fresh_reversals[tid] = rec
            except (TypeError, KeyError, ValueError):
                continue
        # Milestones + advisor state don't TTL-prune — they live as long as
        # the trade is open (cleanup happens when the trade closes).
        alerted_milestones = {
            tid: [float(x) for x in lst if isinstance(x, (int, float))]
            for tid, lst in (raw.get("alerted_milestones") or {}).items()
        }
        last_advisor_at = dict(raw.get("last_advisor_at") or {})
        return cls(
            alerted_reversals=fresh_reversals,
            alerted_milestones=alerted_milestones,
            last_advisor_at=last_advisor_at,
            path=p,
        )

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = {
            "alerted_reversals": {
                tid: asdict(rec) for tid, rec in self.alerted_reversals.items()
            },
            "alerted_milestones": self.alerted_milestones,
            "last_advisor_at": self.last_advisor_at,
        }
        tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

    # Reversal idempotency
    def already_alerted(self, trade_id: int, reverse_to: str) -> bool:
        rec = self.alerted_reversals.get(str(trade_id))
        if rec is None:
            return False
        return rec.reverse_to == reverse_to

    def record(self, trade_id: int, reverse_to: str) -> None:
        self.alerted_reversals[str(trade_id)] = ReversalAlertRecord(
            alerted_at=_now_utc().isoformat(),
            reverse_to=reverse_to,
        )

    # Milestone idempotency
    def milestone_alerted(self, trade_id: int, threshold: float) -> bool:
        return threshold in self.alerted_milestones.get(str(trade_id), [])

    def record_milestone(self, trade_id: int, threshold: float) -> None:
        key = str(trade_id)
        self.alerted_milestones.setdefault(key, []).append(threshold)

    # Advisor scheduling
    def advisor_due(self, trade_id: int, *, interval_hours: float) -> bool:
        """True if no advisor run for this trade OR last run is older than interval."""
        last = self.last_advisor_at.get(str(trade_id))
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return _now_utc() - last_dt >= timedelta(hours=interval_hours)

    def record_advisor_run(self, trade_id: int) -> None:
        self.last_advisor_at[str(trade_id)] = _now_utc().isoformat()


# --- Event types ---
@dataclass
class MonitorEvent:
    kind: str  # "sl_hit" | "tp_hit" | "trend_reversal"
    trade_id: int
    symbol: str
    direction: str
    detail: dict[str, Any]


@dataclass
class OpenTradeStatus:
    """Live snapshot for one open trade — used by /status command."""

    trade_id: int
    symbol: str
    direction: str
    filled_entry: float
    filled_sl: float
    filled_tp: float
    position_size: float | None
    current_price: float
    unrealized_pnl_pct: float
    unrealized_pnl_usd: float | None
    distance_to_sl_pct: float  # positive = trade has room before SL hits
    distance_to_tp_pct: float  # positive = trade has room before TP hits


# --- Dispatcher protocols (decouple monitor from notify.py specifics) ---
class TextDispatcher(Protocol):
    def __call__(self, message: str) -> None: ...


class ButtonDispatcher(Protocol):
    def __call__(
        self, message: str, inline_keyboard: list[list[dict]],
    ) -> None: ...


# --- SL/TP touch ---
def _fetch_latest_1m(symbol: str) -> pd.DataFrame:
    return fetch_klines(symbol, "1m", TOUCH_CHECK_KLINE_LIMIT, drop_unclosed=False)


def check_sl_tp_touches(
    conn,
    *,
    dispatcher: TextDispatcher | None = None,
    kline_fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> list[MonitorEvent]:
    """Auto-close any open trade whose SL or TP was touched in the last few minutes.

    Detection uses the high/low of the last few 1m candles. If both SL and TP
    appear touched within the same window (whipsaw), SL takes precedence —
    that's the conservative assumption since SL is closer to entry.

    Exit price written to DB = the SL or TP price itself (where the stop would
    have filled), not the candle's close. Notes the price may differ from
    real exchange fill due to slippage; user can `/correct` after.
    """
    if kline_fetcher is None:
        kline_fetcher = _fetch_latest_1m
    events: list[MonitorEvent] = []
    for trade in open_trades(conn):
        try:
            klines = kline_fetcher(trade["symbol"])
        except BinanceError:
            continue  # don't let one symbol's API hiccup stop the whole monitor
        if klines.empty:
            continue
        high_max = float(klines["high"].max())
        low_min = float(klines["low"].min())
        sl = trade["filled_sl"]
        tp = trade["filled_tp"]
        direction = trade["direction"]

        if direction == "long":
            sl_hit, tp_hit = low_min <= sl, high_max >= tp
        else:
            sl_hit, tp_hit = high_max >= sl, low_min <= tp

        if not sl_hit and not tp_hit:
            continue

        if sl_hit:
            kind, exit_price, reason = "sl_hit", sl, "sl_auto"
        else:
            kind, exit_price, reason = "tp_hit", tp, "tp_auto"

        closed = close_trade(
            conn, trade_id=trade["id"], exit_price=exit_price, close_reason=reason,
        )
        events.append(
            MonitorEvent(
                kind=kind,
                trade_id=trade["id"],
                symbol=trade["symbol"],
                direction=direction,
                detail={
                    "exit_price": exit_price,
                    "pnl_pct": closed["pnl_pct"],
                    "pnl_usd": closed.get("pnl_usd"),
                },
            )
        )
        if dispatcher:
            icon = "🟢" if kind == "tp_hit" else "🔴"
            pnl_str = f"{closed['pnl_pct']:+.2f}%"
            if closed.get("pnl_usd") is not None:
                pnl_str += f" (${closed['pnl_usd']:+.2f})"
            dispatcher(
                f"{icon} Trade #{trade['id']} 자동 종료\n"
                f"   {trade['symbol']} {direction.upper()}\n"
                f"   {'TP' if kind == 'tp_hit' else 'SL'} hit @ {exit_price:.2f}\n"
                f"   PnL: {pnl_str}\n"
                f"   (실제 fill 가격이 다르면 /correct {trade['id']} <price>)"
            )
    return events


# --- Trend reversal ---
def _current_4h_trend(df_4h: pd.DataFrame) -> str:
    """Returns 'long' / 'short' / 'neutral' based on 4H Layer 1.

    Uses layer_1_trend with both directions — if either passes, trend is that
    direction. If both fail (weak gap), trend = 'neutral'.
    """
    df = add_ma(df_4h, [25, 99])
    long_r = layer_1_trend(df, "long")
    if long_r.status == "pass":
        return "long"
    short_r = layer_1_trend(df, "short")
    if short_r.status == "pass":
        return "short"
    return "neutral"


def _fetch_4h(symbol: str) -> pd.DataFrame:
    return fetch_klines(symbol, "4h", 100)


def check_trend_reversals(
    conn,
    state: MonitorState,
    *,
    dispatcher_with_buttons: ButtonDispatcher | None = None,
    klines_4h_fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> list[MonitorEvent]:
    """Alert when 4H trend flipped against an open trade's direction.

    Idempotency: don't re-alert the same (trade_id, new-trend) within
    REVERSAL_TTL_HOURS. If trend recovers (no longer opposite), the alert
    record stays in state but won't fire again unless trend reverses anew.
    """
    if klines_4h_fetcher is None:
        klines_4h_fetcher = _fetch_4h
    rows = open_trades(conn)
    if not rows:
        return []
    # One API call per unique symbol
    symbols = {r["symbol"] for r in rows}
    trends: dict[str, str] = {}
    for sym in symbols:
        try:
            df = klines_4h_fetcher(sym)
        except BinanceError:
            continue
        if df.empty:
            continue
        trends[sym] = _current_4h_trend(df)

    events: list[MonitorEvent] = []
    for trade in rows:
        current = trends.get(trade["symbol"])
        if current is None:
            continue
        opposite = "short" if trade["direction"] == "long" else "long"
        if current != opposite:
            continue
        tid = trade["id"]
        if state.already_alerted(tid, current):
            continue
        events.append(
            MonitorEvent(
                kind="trend_reversal",
                trade_id=tid,
                symbol=trade["symbol"],
                direction=trade["direction"],
                detail={"new_trend": current},
            )
        )
        if dispatcher_with_buttons:
            text = (
                f"⚠️ 추세 반전 — Trade #{tid}\n"
                f"   {trade['symbol']} (당신은 {trade['direction'].upper()} 진입 중)\n"
                f"   4H 추세가 {current.upper()}로 flip\n"
                f"   → trade 전제가 무너졌습니다. 청산 검토 권장."
            )
            buttons = [
                [{"text": "🔴 즉시 종료", "callback_data": f"close_now:{tid}"}],
                [{"text": "👀 관찰 유지 (6h 묵음)", "callback_data": f"observe:{tid}"}],
            ]
            dispatcher_with_buttons(text, buttons)
        state.record(tid, current)
    return events


# --- /status: live unrealized PnL ---
def _fetch_latest_close_1m(symbol: str) -> pd.DataFrame:
    return fetch_klines(symbol, "1m", 1, drop_unclosed=False)


def compute_open_trade_status(
    conn,
    *,
    kline_fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> list[OpenTradeStatus]:
    """Snapshot every open trade's unrealized PnL + distance to SL/TP.

    Used by /status command in the Telegram listener. No DB writes.
    Skips trades whose symbol fails to fetch (don't crash on partial API issue).
    """
    if kline_fetcher is None:
        kline_fetcher = _fetch_latest_close_1m
    out: list[OpenTradeStatus] = []
    for trade in open_trades(conn):
        try:
            df = kline_fetcher(trade["symbol"])
        except BinanceError:
            continue
        if df.empty:
            continue
        current = float(df.iloc[-1]["close"])
        entry, sl, tp = trade["filled_entry"], trade["filled_sl"], trade["filled_tp"]
        size = trade["position_size"]

        if trade["direction"] == "long":
            pnl_pct = (current - entry) / entry * 100
            dist_sl = (current - sl) / current * 100
            dist_tp = (tp - current) / current * 100
        else:
            pnl_pct = (entry - current) / entry * 100
            dist_sl = (sl - current) / current * 100
            dist_tp = (current - tp) / current * 100
        pnl_usd = (pnl_pct / 100) * size if size else None
        out.append(
            OpenTradeStatus(
                trade_id=trade["id"],
                symbol=trade["symbol"],
                direction=trade["direction"],
                filled_entry=entry,
                filled_sl=sl,
                filled_tp=tp,
                position_size=size,
                current_price=current,
                unrealized_pnl_pct=pnl_pct,
                unrealized_pnl_usd=pnl_usd,
                distance_to_sl_pct=dist_sl,
                distance_to_tp_pct=dist_tp,
            )
        )
    return out


# --- Phase 6: PnL milestone alerts ---
def _current_pnl_pct(direction: str, entry: float, current: float) -> float:
    if direction == "long":
        return (current - entry) / entry * 100
    return (entry - current) / entry * 100


def check_pnl_milestones(
    conn,
    state: MonitorState,
    *,
    dispatcher_with_buttons: ButtonDispatcher | None = None,
    kline_fetcher: Callable[[str], pd.DataFrame] | None = None,
    milestones: tuple[float, ...] = PNL_MILESTONES,
) -> list[MonitorEvent]:
    """Detect PnL crossing predefined thresholds; alert with action buttons.

    Per (trade_id, milestone), only fires once. Crossings detected:
    - positive milestones (1, 2, 5): current_pnl_pct >= threshold
    - negative milestones (-1, -2, -5): current_pnl_pct <= threshold

    Once alerted, the milestone stays in state until trade closes (cleanup
    happens when listener's /close fires — see `clear_trade_state`).
    """
    if kline_fetcher is None:
        kline_fetcher = _fetch_latest_close_1m
    events: list[MonitorEvent] = []
    for trade in open_trades(conn):
        try:
            df = kline_fetcher(trade["symbol"])
        except BinanceError:
            continue
        if df.empty:
            continue
        current = float(df.iloc[-1]["close"])
        pnl_pct = _current_pnl_pct(
            trade["direction"], trade["filled_entry"], current,
        )
        for threshold in milestones:
            crossed = (
                (threshold > 0 and pnl_pct >= threshold)
                or (threshold < 0 and pnl_pct <= threshold)
            )
            if not crossed:
                continue
            if state.milestone_alerted(trade["id"], threshold):
                continue
            events.append(
                MonitorEvent(
                    kind="pnl_milestone",
                    trade_id=trade["id"],
                    symbol=trade["symbol"],
                    direction=trade["direction"],
                    detail={
                        "threshold": threshold,
                        "pnl_pct": pnl_pct,
                        "current_price": current,
                    },
                )
            )
            if dispatcher_with_buttons:
                tid = trade["id"]
                icon = "🟢" if threshold > 0 else "🔴"
                sign = "+" if threshold > 0 else ""
                text = (
                    f"{icon} Trade #{tid} {sign}{threshold:.0f}% PnL 도달\n"
                    f"   {trade['symbol']} {trade['direction'].upper()}\n"
                    f"   entry={trade['filled_entry']:.2f} → now={current:.2f}\n"
                    f"   현재 PnL: {pnl_pct:+.2f}%\n"
                    f"   → 어떻게 할까요?"
                )
                buttons = [
                    [{"text": "🔴 즉시 종료", "callback_data": f"close_now:{tid}"}],
                    [{"text": "👀 hold", "callback_data": f"hold:{tid}"}],
                ]
                dispatcher_with_buttons(text, buttons)
            state.record_milestone(trade["id"], threshold)
    return events


# --- Phase 6: position advisor (LLM HOLD/TIGHTEN/PARTIAL/EXIT) ---
def _fetch_4h_for_advisor(symbol: str) -> pd.DataFrame:
    return fetch_klines(symbol, "4h", 100)


def _fetch_1h_for_advisor(symbol: str) -> pd.DataFrame:
    return fetch_klines(symbol, "1h", 100)


def _fetch_15m_for_advisor(symbol: str) -> pd.DataFrame:
    return fetch_klines(symbol, "15m", 100)


def check_position_advisor(
    conn,
    state: MonitorState,
    *,
    dispatcher_with_buttons: ButtonDispatcher | None = None,
    interval_hours: float = ADVISOR_INTERVAL_HOURS,
    current_price_fetcher: Callable[[str], pd.DataFrame] | None = None,
    df_4h_fetcher: Callable[[str], pd.DataFrame] | None = None,
    df_1h_fetcher: Callable[[str], pd.DataFrame] | None = None,
    df_15m_fetcher: Callable[[str], pd.DataFrame] | None = None,
    advisor_evaluate: Callable | None = None,
) -> list[MonitorEvent]:
    """For each open trade due for advisor (interval elapsed), run the LLM
    advisor and send HOLD/TIGHTEN/PARTIAL/EXIT recommendation.

    Imports `agents.position_advisor` lazily so the module remains importable
    in test envs without the agentic stack."""
    if current_price_fetcher is None:
        current_price_fetcher = _fetch_latest_close_1m
    if df_4h_fetcher is None:
        df_4h_fetcher = _fetch_4h_for_advisor
    if df_1h_fetcher is None:
        df_1h_fetcher = _fetch_1h_for_advisor
    if df_15m_fetcher is None:
        df_15m_fetcher = _fetch_15m_for_advisor
    if advisor_evaluate is None:
        from agents.position_advisor import evaluate as _ev
        advisor_evaluate = _ev

    events: list[MonitorEvent] = []
    for trade in open_trades(conn):
        if not state.advisor_due(trade["id"], interval_hours=interval_hours):
            continue
        try:
            df_price = current_price_fetcher(trade["symbol"])
            if df_price.empty:
                continue
            current = float(df_price.iloc[-1]["close"])
            df_4h = df_4h_fetcher(trade["symbol"])
            df_1h = df_1h_fetcher(trade["symbol"])
            df_15m = df_15m_fetcher(trade["symbol"])
        except BinanceError:
            continue
        pnl_pct = _current_pnl_pct(
            trade["direction"], trade["filled_entry"], current,
        )
        result = advisor_evaluate(
            symbol=trade["symbol"],
            direction=trade["direction"],
            entry=trade["filled_entry"],
            current_price=current,
            pnl_pct=pnl_pct,
            df_4h=df_4h,
            df_1h=df_1h,
            df_15m=df_15m,
        )
        # Always record the run timestamp so we don't retry every cron tick on failure
        state.record_advisor_run(trade["id"])
        if result.failed:
            continue
        events.append(
            MonitorEvent(
                kind="advisor",
                trade_id=trade["id"],
                symbol=trade["symbol"],
                direction=trade["direction"],
                detail={
                    "action": result.action,
                    "confidence": result.confidence,
                    "rationale": result.rationale,
                    "pnl_pct": pnl_pct,
                },
            )
        )
        if dispatcher_with_buttons:
            tid = trade["id"]
            action_icon = {
                "HOLD": "👀", "TIGHTEN": "🔒",
                "PARTIAL": "✂️", "EXIT": "🔴",
            }.get(result.action, "📊")
            text = (
                f"{action_icon} Trade #{tid} advisor 평가 → {result.action}\n"
                f"   {trade['symbol']} {trade['direction'].upper()}\n"
                f"   entry={trade['filled_entry']:.2f} → now={current:.2f}\n"
                f"   PnL: {pnl_pct:+.2f}%  confidence: {result.confidence}/10\n"
                f"   사유: {result.rationale}"
            )
            buttons = [
                [{"text": "🔴 즉시 종료", "callback_data": f"close_now:{tid}"}],
                [{"text": "👀 hold", "callback_data": f"hold:{tid}"}],
            ]
            dispatcher_with_buttons(text, buttons)
    return events


# --- On-demand: force the advisor on one trade right now (Telegram /advise) ---
def force_advise(
    trade_id: int,
    *,
    db_path: Path | None = None,
    current_price_fetcher: Callable[[str], pd.DataFrame] | None = None,
    df_4h_fetcher: Callable[[str], pd.DataFrame] | None = None,
    df_1h_fetcher: Callable[[str], pd.DataFrame] | None = None,
    df_15m_fetcher: Callable[[str], pd.DataFrame] | None = None,
    advisor_evaluate: Callable | None = None,
) -> str:
    """Run the LLM advisor on a specific trade immediately, ignoring the
    4h schedule. Does NOT touch monitor state (the next scheduled tick will
    still happen on its own cadence). Returns a Telegram-friendly text.
    """
    if db_path is None:
        db_path = Path(os.environ.get("JOURNAL_DB", str(DEFAULT_DB_PATH)))
    if current_price_fetcher is None:
        current_price_fetcher = _fetch_latest_close_1m
    if df_4h_fetcher is None:
        df_4h_fetcher = _fetch_4h_for_advisor
    if df_1h_fetcher is None:
        df_1h_fetcher = _fetch_1h_for_advisor
    if df_15m_fetcher is None:
        df_15m_fetcher = _fetch_15m_for_advisor
    if advisor_evaluate is None:
        from agents.position_advisor import evaluate as _ev
        advisor_evaluate = _ev

    with connect(db_path) as conn:
        rows = [t for t in open_trades(conn) if t["id"] == trade_id]
    if not rows:
        return f"❌ Trade #{trade_id} 없음 (또는 이미 종료됨)"
    trade = rows[0]

    try:
        df_price = current_price_fetcher(trade["symbol"])
        if df_price.empty:
            return f"❌ {trade['symbol']} 가격 fetch 결과 비어있음"
        current = float(df_price.iloc[-1]["close"])
        df_4h = df_4h_fetcher(trade["symbol"])
        df_1h = df_1h_fetcher(trade["symbol"])
        df_15m = df_15m_fetcher(trade["symbol"])
    except BinanceError as e:
        return f"❌ Binance fetch 실패: {e}"

    pnl_pct = _current_pnl_pct(
        trade["direction"], trade["filled_entry"], current,
    )
    result = advisor_evaluate(
        symbol=trade["symbol"],
        direction=trade["direction"],
        entry=trade["filled_entry"],
        current_price=current,
        pnl_pct=pnl_pct,
        df_4h=df_4h, df_1h=df_1h, df_15m=df_15m,
    )
    if result.failed:
        return (
            f"❌ Trade #{trade_id} advisor 실패: {result.failure_reason}\n"
            f"   {trade['symbol']} {trade['direction'].upper()} entry={trade['filled_entry']:.2f} "
            f"→ now={current:.2f} PnL={pnl_pct:+.2f}%"
        )
    action_icon = {
        "HOLD": "👀", "TIGHTEN": "🔒", "PARTIAL": "✂️", "EXIT": "🔴",
    }.get(result.action, "📊")
    return (
        f"{action_icon} Trade #{trade_id} advisor 평가 (force) → {result.action}\n"
        f"   {trade['symbol']} {trade['direction'].upper()}\n"
        f"   entry={trade['filled_entry']:.2f} → now={current:.2f}\n"
        f"   PnL: {pnl_pct:+.2f}%  confidence: {result.confidence}/10\n"
        f"   사유: {result.rationale}"
    )


# --- Orchestrator (called from scan.py cron) ---
def run_position_monitor(
    db_path: Path | None = None,
    state: MonitorState | None = None,
    *,
    dispatcher: TextDispatcher | None = None,
    dispatcher_with_buttons: ButtonDispatcher | None = None,
) -> list[MonitorEvent]:
    """Single entrypoint for cron — runs SL/TP, reversal, milestone, advisor
    checks; saves state. Returns combined event list.

    `db_path` defaults to env JOURNAL_DB then journal.db. Set MONITOR_DISABLED=1
    to no-op (used by the default test environment)."""
    if os.environ.get("MONITOR_DISABLED") == "1":
        return []
    if db_path is None:
        db_path = Path(os.environ.get("JOURNAL_DB", str(DEFAULT_DB_PATH)))
    state = state or MonitorState.load()
    with connect(db_path) as conn:
        sl_tp_events = check_sl_tp_touches(conn, dispatcher=dispatcher)
        reversal_events = check_trend_reversals(
            conn, state, dispatcher_with_buttons=dispatcher_with_buttons,
        )
        milestone_events = check_pnl_milestones(
            conn, state, dispatcher_with_buttons=dispatcher_with_buttons,
        )
        # Advisor calls the LLM and can be slow + costs token quota.
        # Wrap in try/except so a broken advisor doesn't kill the monitor.
        try:
            advisor_events = check_position_advisor(
                conn, state, dispatcher_with_buttons=dispatcher_with_buttons,
            )
        except Exception as e:
            import sys
            print(f"[monitor] advisor error: {type(e).__name__}: {e}", file=sys.stderr)
            advisor_events = []
    state.save()
    return sl_tp_events + reversal_events + milestone_events + advisor_events


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
