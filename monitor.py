"""Position monitor — sustains the trade after entry (Phase 5).

For every open trade in journal.db, this module:
  1. checks the latest 1m candles for SL/TP touch → auto-closes + alerts
  2. checks 4H Layer 1 trend; if it reversed against the trade direction,
     sends a "추세 반전" alert with inline buttons (idempotent: alerts once
     per (trade_id, new-trend-direction) within TTL)
  3. computes live unrealized PnL for /status command (no side effects)

Idempotency state: `.tv-monitor-state.json` records which reversal alerts
have fired per trade. Pruned on load past `REVERSAL_TTL_HOURS`.

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


# --- Monitor state (idempotency for trend reversal alerts) ---
@dataclass
class ReversalAlertRecord:
    """One row per (trade_id, new-trend) we've already alerted on."""

    alerted_at: str  # ISO 8601, UTC
    reverse_to: str  # "long" or "short" or "neutral"


@dataclass
class MonitorState:
    """Persisted state for reversal-alert idempotency.

    Schema: {"alerted_reversals": {"<trade_id>": {"alerted_at": ..., "reverse_to": ...}}}
    """

    alerted_reversals: dict[str, ReversalAlertRecord] = field(default_factory=dict)
    path: Path = field(default_factory=lambda: DEFAULT_MONITOR_STATE_PATH)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_MONITOR_STATE_PATH) -> "MonitorState":
        p = Path(path)
        if not p.exists():
            return cls(alerted_reversals={}, path=p)
        raw = json.loads(p.read_text())
        cutoff = _now_utc() - timedelta(hours=REVERSAL_TTL_HOURS)
        fresh: dict[str, ReversalAlertRecord] = {}
        for tid, info in (raw.get("alerted_reversals") or {}).items():
            try:
                rec = ReversalAlertRecord(**info)
                if datetime.fromisoformat(rec.alerted_at) >= cutoff:
                    fresh[tid] = rec
            except (TypeError, KeyError, ValueError):
                continue
        return cls(alerted_reversals=fresh, path=p)

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = {
            "alerted_reversals": {
                tid: asdict(rec) for tid, rec in self.alerted_reversals.items()
            }
        }
        tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

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


# --- Orchestrator (called from scan.py cron) ---
def run_position_monitor(
    db_path: Path | None = None,
    state: MonitorState | None = None,
    *,
    dispatcher: TextDispatcher | None = None,
    dispatcher_with_buttons: ButtonDispatcher | None = None,
) -> list[MonitorEvent]:
    """Single entrypoint for cron — runs both SL/TP and reversal checks,
    saves state. Returns combined event list.

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
    state.save()
    return sl_tp_events + reversal_events


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
