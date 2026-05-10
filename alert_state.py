"""Alert idempotency — track which setups we've already alerted on.

A setup is identified by (symbol, direction, sl_swing_price). Two prices are
considered the same swing if they're within 0.1% of each other (BTC moves
constantly, so exact-equality is too strict).

Alerts older than ALERT_TTL_HOURS are pruned on load — same setup that's been
quiet for that long can re-alert if it triggers again.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

DEFAULT_STATE_PATH: Final = Path(".tv-state.json")
SWING_PRICE_TOLERANCE_PCT: Final[float] = 0.001  # 0.1%
ALERT_TTL_HOURS: Final[int] = 24


@dataclass
class AlertRecord:
    symbol: str
    direction: str
    sl_swing_price: float
    alerted_at: str  # ISO 8601, UTC

    def alerted_dt(self) -> datetime:
        return datetime.fromisoformat(self.alerted_at)


@dataclass
class AlertState:
    """In-memory list of recent alerts, persisted to JSON on save()."""

    alerts: list[AlertRecord] = field(default_factory=list)
    path: Path = field(default_factory=lambda: DEFAULT_STATE_PATH)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_STATE_PATH) -> "AlertState":
        """Load state from JSON. Missing file → empty state. Prunes stale entries."""
        p = Path(path)
        if not p.exists():
            return cls(alerts=[], path=p)
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        records = [AlertRecord(**r) for r in raw.get("alerts", [])]
        cutoff = _now_utc() - timedelta(hours=ALERT_TTL_HOURS)
        fresh = [r for r in records if r.alerted_dt() >= cutoff]
        return cls(alerts=fresh, path=p)

    def save(self) -> None:
        """Atomic write: tmp → rename."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = {"alerts": [asdict(r) for r in self.alerts]}
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(body, f, indent=2)
        tmp.replace(self.path)

    def already_alerted(
        self,
        symbol: str,
        direction: str,
        sl_swing_price: float,
        *,
        ttl_hours: int = ALERT_TTL_HOURS,
    ) -> bool:
        """True if a same-symbol, same-direction, same-swing alert exists within TTL."""
        cutoff = _now_utc() - timedelta(hours=ttl_hours)
        for r in self.alerts:
            if r.symbol != symbol or r.direction != direction:
                continue
            if r.alerted_dt() < cutoff:
                continue
            if _same_price(r.sl_swing_price, sl_swing_price):
                return True
        return False

    def record(self, symbol: str, direction: str, sl_swing_price: float) -> None:
        """Append a new alert record (caller is responsible for save())."""
        self.alerts.append(
            AlertRecord(
                symbol=symbol,
                direction=direction,
                sl_swing_price=sl_swing_price,
                alerted_at=_now_utc().isoformat(),
            )
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _same_price(a: float, b: float) -> bool:
    if a == 0 or b == 0:
        return a == b
    return abs(a - b) / abs(a) <= SWING_PRICE_TOLERANCE_PCT
