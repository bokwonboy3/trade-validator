"""TTL cache for specialist agent outputs.

Keyed on ``(symbol, direction, sl_swing_price, layer_3_status)`` with the
same 24h TTL and 0.1% swing-price tolerance as ``AlertState``. Persisted as
JSON next to the alert-state file so the two share the same lifecycle.

Why ``layer_3_status`` is part of the key: the microstructure specialist
receives ``layer_3.status`` directly in its prompt, so a verdict produced
under FORMING context (L3 = fail/pending) carries a "rejection unconfirmed"
rationale that becomes stale the moment L3 transitions to "pass". Keying on
L3 status forces a fresh agent call on that transition; other cache-hit
paths (state file reset, restart) still serve from cache.

Why this exists at all: a single agentic analysis = 5 LLM calls (~15 s
parallel). Without caching, every cron tick that re-enters the dispatch
path on a setup whose key hasn't changed burns the full cost again. Agent
calls are now gated on ``state.already_alerted() is False`` in scan.py, so
the cache is defense-in-depth for cases where the alert-state gate is
bypassed (manual reset, file corruption, future code paths).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from agents.types import AgentVerdict, SpecialistOutput
from alert_state import ALERT_TTL_HOURS, SWING_PRICE_TOLERANCE_PCT

DEFAULT_CACHE_PATH: Final = Path(".tv-agent-cache.json")


@dataclass
class CacheEntry:
    symbol: str
    direction: str
    sl_swing_price: float
    layer_3_status: str  # "pass" / "fail" / "pending" — segregates forming vs confirmed rationales
    cached_at: str  # ISO 8601, UTC
    verdict: dict  # serialized AgentVerdict
    specialists: list[dict]  # serialized list[SpecialistOutput]

    def cached_dt(self) -> datetime:
        return datetime.fromisoformat(self.cached_at)


@dataclass
class SpecialistCache:
    entries: list[CacheEntry] = field(default_factory=list)
    path: Path = field(default_factory=lambda: DEFAULT_CACHE_PATH)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CACHE_PATH) -> "SpecialistCache":
        """Load cache from JSON. Missing file → empty cache. Prunes stale entries."""
        p = Path(path)
        if not p.exists():
            return cls(entries=[], path=p)
        try:
            with p.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupted cache is non-fatal — treat as empty and let the next
            # save() rewrite the file cleanly.
            return cls(entries=[], path=p)
        # Drop entries missing the new layer_3_status field — they predate the
        # schema bump and we can't safely classify them.
        entries: list[CacheEntry] = []
        for e in raw.get("entries", []):
            if "layer_3_status" not in e:
                continue
            entries.append(CacheEntry(**e))
        cutoff = _now_utc() - timedelta(hours=ALERT_TTL_HOURS)
        fresh = [e for e in entries if e.cached_dt() >= cutoff]
        return cls(entries=fresh, path=p)

    def save(self) -> None:
        """Atomic write: tmp → rename."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = {"entries": [dataclasses.asdict(e) for e in self.entries]}
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(body, f, indent=2)
        tmp.replace(self.path)

    def get(
        self,
        symbol: str,
        direction: str,
        sl_swing_price: float,
        layer_3_status: str,
        *,
        ttl_hours: int = ALERT_TTL_HOURS,
    ) -> tuple[AgentVerdict, list[SpecialistOutput]] | None:
        """Return cached (verdict, specialists) for matching key, or None."""
        cutoff = _now_utc() - timedelta(hours=ttl_hours)
        for e in self.entries:
            if e.symbol != symbol or e.direction != direction:
                continue
            if e.layer_3_status != layer_3_status:
                continue
            if e.cached_dt() < cutoff:
                continue
            if _same_price(e.sl_swing_price, sl_swing_price):
                return (
                    _deserialize_verdict(e.verdict),
                    [_deserialize_specialist(s) for s in e.specialists],
                )
        return None

    def put(
        self,
        symbol: str,
        direction: str,
        sl_swing_price: float,
        layer_3_status: str,
        verdict: AgentVerdict,
        specialists: list[SpecialistOutput],
    ) -> None:
        """Insert or replace the entry for this key (caller does save())."""
        self.entries = [
            e for e in self.entries
            if not (
                e.symbol == symbol
                and e.direction == direction
                and e.layer_3_status == layer_3_status
                and _same_price(e.sl_swing_price, sl_swing_price)
            )
        ]
        self.entries.append(
            CacheEntry(
                symbol=symbol,
                direction=direction,
                sl_swing_price=sl_swing_price,
                layer_3_status=layer_3_status,
                cached_at=_now_utc().isoformat(),
                verdict=_serialize_verdict(verdict),
                specialists=[_serialize_specialist(s) for s in specialists],
            )
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _same_price(a: float, b: float) -> bool:
    if a == 0 or b == 0:
        return a == b
    return abs(a - b) / abs(a) <= SWING_PRICE_TOLERANCE_PCT


def _serialize_verdict(v: AgentVerdict) -> dict:
    return dataclasses.asdict(v)


def _deserialize_verdict(d: dict) -> AgentVerdict:
    return AgentVerdict(**d)


def _serialize_specialist(s: SpecialistOutput) -> dict:
    return dataclasses.asdict(s)


def _deserialize_specialist(d: dict) -> SpecialistOutput:
    return SpecialistOutput(**d)
