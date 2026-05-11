"""SQLite trade journal — Phase 4.

Schema:
  alerts        — every dispatched scanner alert (auto-populated)
  trades        — trades the user took (filled, with outcome)
  skipped       — alerts the user intentionally passed on (with reason)

Single-file SQLite DB at ./journal.db (gitignored). No external deps —
sqlite3 ships with Python.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

DEFAULT_DB_PATH: Final = Path("journal.db")

# --- Schema ---
SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS alerts (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    tier            TEXT NOT NULL,
    entry           REAL NOT NULL,
    sl              REAL NOT NULL,
    tp              REAL NOT NULL,
    tier1_score     INTEGER NOT NULL,
    agent_verdict   TEXT,
    agent_confidence INTEGER,
    agent_downgraded INTEGER DEFAULT 0,
    raw_record      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id        TEXT NOT NULL REFERENCES alerts(id),
    status          TEXT NOT NULL DEFAULT 'open',
    filled_entry    REAL NOT NULL,
    filled_sl       REAL NOT NULL,
    filled_tp       REAL NOT NULL,
    position_size   REAL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    exit_price      REAL,
    close_reason    TEXT,
    pnl_pct         REAL,
    pnl_usd         REAL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS skipped (
    alert_id        TEXT PRIMARY KEY REFERENCES alerts(id),
    reason          TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
"""


@contextmanager
def connect(path: Path = DEFAULT_DB_PATH):
    """Open + initialize the DB, yield connection, close at end."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- Insert / Update ---
def insert_alert_idempotent(conn: sqlite3.Connection, record: dict) -> None:
    """Insert one alert. If id already exists, skip (idempotent migration)."""
    raw_json = json.dumps(record, ensure_ascii=False)
    conn.execute(
        """
        INSERT OR IGNORE INTO alerts
            (id, timestamp, symbol, direction, tier, entry, sl, tp,
             tier1_score, agent_verdict, agent_confidence, agent_downgraded, raw_record)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["alert_id"],
            record["ts"],
            record["symbol"],
            record["direction"],
            record["tier"],
            float(record["entry"]),
            float(record["sl"]),
            float(record["tp"]),
            int(record["tier1_score"]),
            record.get("agent_verdict"),
            record.get("agent_confidence"),
            1 if record.get("agent_downgraded") else 0,
            raw_json,
        ),
    )


def record_trade(
    conn: sqlite3.Connection,
    *,
    alert_id: str,
    filled_entry: float,
    filled_sl: float,
    filled_tp: float,
    position_size: float | None = None,
    notes: str = "",
) -> int:
    """Record a new open trade tied to an alert. Returns the trade id."""
    cur = conn.execute(
        """
        INSERT INTO trades
            (alert_id, status, filled_entry, filled_sl, filled_tp,
             position_size, opened_at, notes)
        VALUES (?, 'open', ?, ?, ?, ?, ?, ?)
        """,
        (
            alert_id, filled_entry, filled_sl, filled_tp,
            position_size, _now_iso(), notes,
        ),
    )
    return cur.lastrowid


def close_trade(
    conn: sqlite3.Connection,
    *,
    trade_id: int,
    exit_price: float,
    close_reason: str = "manual",
    notes: str = "",
) -> dict:
    """Close an open trade, compute PnL. Returns the closed row."""
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError(f"trade id {trade_id} not found")
    if row["status"] != "open":
        raise ValueError(f"trade {trade_id} is already {row['status']}")

    # Get direction from the linked alert
    alert = conn.execute(
        "SELECT direction FROM alerts WHERE id = ?", (row["alert_id"],)
    ).fetchone()
    direction = alert["direction"] if alert else "long"

    entry = row["filled_entry"]
    if direction == "long":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100

    size = row["position_size"]
    pnl_usd = (pnl_pct / 100) * size if size else None

    extra_notes = (row["notes"] or "") + (("; " + notes) if notes else "")
    conn.execute(
        """
        UPDATE trades SET status='closed', closed_at=?, exit_price=?,
            close_reason=?, pnl_pct=?, pnl_usd=?, notes=?
        WHERE id = ?
        """,
        (_now_iso(), exit_price, close_reason, pnl_pct, pnl_usd, extra_notes, trade_id),
    )
    return dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())


def record_skip(conn: sqlite3.Connection, *, alert_id: str, reason: str) -> None:
    """Mark an alert as intentionally skipped."""
    conn.execute(
        """
        INSERT OR REPLACE INTO skipped (alert_id, reason, timestamp)
        VALUES (?, ?, ?)
        """,
        (alert_id, reason, _now_iso()),
    )


# --- Queries ---
def list_alerts(
    conn: sqlite3.Connection, *, limit: int = 20, symbol: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM alerts"
    args: list = []
    if symbol:
        sql += " WHERE symbol = ?"
        args.append(symbol)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def open_trades(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT t.*, a.symbol, a.direction FROM trades t "
        "JOIN alerts a ON t.alert_id = a.id "
        "WHERE t.status = 'open' ORDER BY t.opened_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def closed_trades(
    conn: sqlite3.Connection, *, days: int = 30,
) -> list[dict]:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT t.*, a.symbol, a.direction, a.tier1_score, a.agent_verdict "
        "FROM trades t JOIN alerts a ON t.alert_id = a.id "
        "WHERE t.status='closed' AND t.closed_at >= ? "
        "ORDER BY t.closed_at DESC",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
