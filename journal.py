"""Trade journal CLI — record what you actually did vs what the scanner suggested.

Workflow:
  python journal.py migrate                  # import alerts.jsonl → DB (once)
  python journal.py list                     # recent alerts
  python journal.py take <alert_id> --entry 80937 --sl 80637 --tp 81837 [--size 100]
  python journal.py skip <alert_id> --reason "macro bearish"
  python journal.py open                     # show open positions
  python journal.py close <trade_id> --price 81600 [--reason tp_hit]
  python journal.py stats [--days 30]        # win rate, avg R, by-specialist
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from data.journal_db import (
    DEFAULT_DB_PATH,
    close_trade,
    closed_trades,
    connect,
    insert_alert_idempotent,
    list_alerts,
    open_trades,
    record_skip,
    record_trade,
)


def cmd_migrate(args: argparse.Namespace) -> int:
    """Import alerts.jsonl into the DB. Idempotent — re-running is safe."""
    src = Path(args.jsonl)
    if not src.exists():
        print(f"❌ {src} 없음. cron이 한 번 알람 dispatch한 후 다시 실행하세요.", file=sys.stderr)
        return 1
    imported = 0
    skipped = 0
    with connect(Path(args.db)) as conn:
        for line in src.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "alert_id" not in record:
                skipped += 1
                continue
            before = conn.total_changes
            insert_alert_idempotent(conn, record)
            if conn.total_changes > before:
                imported += 1
    print(f"✅ {imported}건 import됨, {skipped}건 skip (alert_id 없음 — 구버전)")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with connect(Path(args.db)) as conn:
        rows = list_alerts(conn, limit=args.limit, symbol=args.symbol)
    if not rows:
        print("알람 없음. `journal migrate` 먼저 실행하세요.")
        return 0
    print(f"=== 최근 {len(rows)}건 알람 ===")
    for r in rows:
        agent = f"{r['agent_verdict']}({r['agent_confidence']}%)" if r['agent_verdict'] else "—"
        print(
            f"  {r['id']}  {r['symbol']:8s}  {r['direction']:5s}  "
            f"score={r['tier1_score']}/5  agent={agent}  "
            f"entry={r['entry']:.2f}"
        )
    return 0


def cmd_take(args: argparse.Namespace) -> int:
    with connect(Path(args.db)) as conn:
        # Verify alert exists
        alert = conn.execute(
            "SELECT id FROM alerts WHERE id = ?", (args.alert_id,)
        ).fetchone()
        if not alert:
            print(f"❌ alert_id {args.alert_id!r} 없음. `journal list`로 확인.", file=sys.stderr)
            return 1
        tid = record_trade(
            conn,
            alert_id=args.alert_id,
            filled_entry=args.entry,
            filled_sl=args.sl,
            filled_tp=args.tp,
            position_size=args.size,
            notes=args.notes or "",
        )
        print(f"✅ trade #{tid} opened on {args.alert_id}")
        print(f"   entry={args.entry} SL={args.sl} TP={args.tp}"
              + (f" size={args.size}" if args.size else ""))
        print(f"   종료 시: python journal.py close {tid} --price <exit_price>")
    return 0


def cmd_skip(args: argparse.Namespace) -> int:
    with connect(Path(args.db)) as conn:
        alert = conn.execute(
            "SELECT id FROM alerts WHERE id = ?", (args.alert_id,)
        ).fetchone()
        if not alert:
            print(f"❌ alert_id {args.alert_id!r} 없음.", file=sys.stderr)
            return 1
        record_skip(conn, alert_id=args.alert_id, reason=args.reason)
        print(f"✅ {args.alert_id} skipped: {args.reason}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    with connect(Path(args.db)) as conn:
        rows = open_trades(conn)
    if not rows:
        print("열린 trade 없음.")
        return 0
    print(f"=== 열린 trade {len(rows)}건 ===")
    for r in rows:
        print(
            f"  #{r['id']}  {r['symbol']} {r['direction']}  "
            f"entry={r['filled_entry']:.2f} SL={r['filled_sl']:.2f} TP={r['filled_tp']:.2f}  "
            f"opened={r['opened_at'][:16]}"
        )
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    with connect(Path(args.db)) as conn:
        try:
            closed = close_trade(
                conn,
                trade_id=args.trade_id,
                exit_price=args.price,
                close_reason=args.reason,
                notes=args.notes or "",
            )
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
    pnl_pct = closed["pnl_pct"]
    sign = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪")
    print(f"✅ trade #{closed['id']} closed")
    print(f"   exit={args.price} reason={args.reason}")
    print(f"   {sign} PnL: {pnl_pct:+.2f}%"
          + (f" (${closed['pnl_usd']:+.2f})" if closed.get('pnl_usd') is not None else ""))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with connect(Path(args.db)) as conn:
        rows = closed_trades(conn, days=args.days)
    if not rows:
        print(f"지난 {args.days}일 동안 종료된 trade 없음.")
        return 0
    wins = [r for r in rows if (r["pnl_pct"] or 0) > 0]
    losses = [r for r in rows if (r["pnl_pct"] or 0) < 0]
    win_rate = len(wins) / len(rows) * 100
    avg_pnl = sum(r["pnl_pct"] or 0 for r in rows) / len(rows)
    avg_win = sum(r["pnl_pct"] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r["pnl_pct"] for r in losses) / len(losses) if losses else 0
    expectancy = (win_rate / 100) * avg_win + ((100 - win_rate) / 100) * avg_loss

    print(f"=== Trade Stats (최근 {args.days}일) ===")
    print(f"  Trades: {len(rows)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Avg PnL: {avg_pnl:+.2f}%")
    print(f"  Avg win: {avg_win:+.2f}%  |  Avg loss: {avg_loss:+.2f}%")
    print(f"  Expectancy: {expectancy:+.2f}% per trade")

    # Group by Tier 1 score
    by_score: dict[int, list[float]] = {}
    for r in rows:
        by_score.setdefault(r["tier1_score"], []).append(r["pnl_pct"] or 0)
    print(f"\n  Tier 1 score별:")
    for score in sorted(by_score):
        pnls = by_score[score]
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {score}/5: {len(pnls)} trades, win_rate={wr:.0f}%, avg={sum(pnls)/len(pnls):+.2f}%")

    # Group by agent verdict
    by_agent: dict[str, list[float]] = {}
    for r in rows:
        av = r.get("agent_verdict") or "—"
        by_agent.setdefault(av, []).append(r["pnl_pct"] or 0)
    if len(by_agent) > 1:
        print(f"\n  Agent verdict별:")
        for av in sorted(by_agent):
            pnls = by_agent[av]
            wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            print(f"    {av}: {len(pnls)} trades, win_rate={wr:.0f}%, avg={sum(pnls)/len(pnls):+.2f}%")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="journal.py",
        description="Trade journal — track actual trades vs scanner predictions",
    )
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("migrate", help="alerts.jsonl → DB import (idempotent)")
    m.add_argument("--jsonl", default="alerts.jsonl")
    m.set_defaults(func=cmd_migrate)

    l = sub.add_parser("list", help="최근 알람 보기")
    l.add_argument("--limit", type=int, default=20)
    l.add_argument("--symbol", default=None)
    l.set_defaults(func=cmd_list)

    t = sub.add_parser("take", help="alert에 대해 trade 진입 기록")
    t.add_argument("alert_id")
    t.add_argument("--entry", type=float, required=True)
    t.add_argument("--sl", type=float, required=True)
    t.add_argument("--tp", type=float, required=True)
    t.add_argument("--size", type=float, default=None, help="포지션 크기 (USD)")
    t.add_argument("--notes", default="")
    t.set_defaults(func=cmd_take)

    s = sub.add_parser("skip", help="alert 의도적 skip 기록")
    s.add_argument("alert_id")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_skip)

    o = sub.add_parser("open", help="현재 열린 trade 목록")
    o.set_defaults(func=cmd_open)

    c = sub.add_parser("close", help="trade 종료 + PnL 기록")
    c.add_argument("trade_id", type=int)
    c.add_argument("--price", type=float, required=True)
    c.add_argument("--reason", default="manual")
    c.add_argument("--notes", default="")
    c.set_defaults(func=cmd_close)

    st = sub.add_parser("stats", help="누적 trade 통계 + framework 정확도")
    st.add_argument("--days", type=int, default=30)
    st.set_defaults(func=cmd_stats)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
