"""Market Memory — persistent context across cron ticks.

Each cron run is otherwise stateless. This module gives specialists a memory
of recent alerts + market events so they can reason about *continuity*
("BTC has been testing $82k for 4 hours") rather than treating every tick as
fresh.

Storage: append-only JSON Lines file. Cheap to write, easy to tail.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

DEFAULT_MEMORY_PATH: Final = Path("market_memory.jsonl")
DEFAULT_LOOKBACK_HOURS: Final = 24
DEFAULT_MAX_ENTRIES: Final = 50  # cap context size for prompt injection


def record_entry(
    *,
    symbol: str,
    tier1_verdict: str,
    agent_verdict: str | None,
    confidence: int | None,
    note: str,
    path: Path = DEFAULT_MEMORY_PATH,
) -> None:
    """Append one event to the memory file. Atomic (single write call)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "tier1": tier1_verdict,
        "agent": agent_verdict,
        "confidence": confidence,
        "note": note,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_recent(
    *,
    hours: int = DEFAULT_LOOKBACK_HOURS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    symbol: str | None = None,
    path: Path = DEFAULT_MEMORY_PATH,
) -> list[dict]:
    """Read recent memory entries (newest first). Optionally filter by symbol."""
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(e["ts"])
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            if symbol and e.get("symbol") != symbol:
                continue
            entries.append(e)
    entries.sort(key=lambda e: e["ts"], reverse=True)
    return entries[:max_entries]


def summarize_for_prompt(
    *,
    symbol: str,
    hours: int = DEFAULT_LOOKBACK_HOURS,
    max_entries: int = 10,
    path: Path = DEFAULT_MEMORY_PATH,
) -> str:
    """Compact human-readable summary of recent events for injection into
    specialist prompts. Returns empty string when no memory."""
    entries = read_recent(hours=hours, max_entries=max_entries, symbol=symbol, path=path)
    if not entries:
        return ""
    lines = [
        f"[{e['ts'][11:16]}Z] tier1={e['tier1']} agent={e.get('agent', '-')} "
        f"conf={e.get('confidence', '-')} — {e.get('note', '')[:80]}"
        for e in entries
    ]
    return f"Recent {symbol} setups (last {hours}h):\n" + "\n".join(lines)
