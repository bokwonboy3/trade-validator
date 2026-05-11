"""Watchlist alerts — symbols approaching a setup but not yet qualifying.

A scan result enters the watchlist when:
  - Layer 2 fails (entry is NOT within ±0.3% of any key level)
  - but the closest level is still within WATCH_DISTANCE_MAX (1.5% by default)

This gives the user advance notice — "BTC is approaching swing_low @ 80,200,
need -0.5% more for the setup to fire" — without dispatching a real CONFIRMED
alert. State is persisted to `.tv-watchlist-state.json` with a 12h TTL so the
same level doesn't ping every 2-minute cron tick.

Idempotency key: ``"{symbol}:{direction}:{rounded_target_price}"``. Rounding
to ~0.1% absorbs minor swing-detection wobble so we don't treat the same
level as a new one across ticks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from scan import ScanResult

DEFAULT_WATCHLIST_STATE_PATH: Final = Path(".tv-watchlist-state.json")

# Layer 2 passes at distance ≤ 0.3%; we watch the band [0.3%, 1.5%].
# Anything beyond 1.5% is too far to call "approaching".
WATCH_DISTANCE_MIN: Final[float] = 0.003
WATCH_DISTANCE_MAX: Final[float] = 0.015
WATCH_TTL_HOURS: Final[int] = 12


@dataclass(frozen=True)
class WatchCandidate:
    symbol: str
    direction: str
    target_price: float
    target_label: str
    current_entry: float
    distance_pct: float  # absolute distance |entry - target| / entry


@dataclass
class WatchlistState:
    """{idempotency_key: ISO8601 alert timestamp}. TTL-pruned on load."""

    alerted: dict[str, str] = field(default_factory=dict)
    path: Path = field(default_factory=lambda: DEFAULT_WATCHLIST_STATE_PATH)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_WATCHLIST_STATE_PATH) -> "WatchlistState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return cls(path=p)
        cutoff = _now_utc() - timedelta(hours=WATCH_TTL_HOURS)
        fresh: dict[str, str] = {}
        for k, v in (raw.get("alerted") or {}).items():
            try:
                ts = datetime.fromisoformat(v)
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                fresh[k] = v
        return cls(alerted=fresh, path=p)

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"alerted": self.alerted}, indent=2))
        tmp.replace(self.path)

    def already_alerted(self, key: str) -> bool:
        return key in self.alerted

    def record(self, key: str) -> None:
        self.alerted[key] = _now_utc().isoformat()


def watch_key(symbol: str, direction: str, target_price: float) -> str:
    """Idempotency key. Target rounded to ~0.1% so micro-swing fluctuations
    don't generate spurious new keys for what's the same support/resistance."""
    if target_price <= 0:
        rounded = target_price
    else:
        # 4 significant figures — 81537 → 81540, 1.234 → 1.234
        magnitude = 10 ** max(0, len(str(int(target_price))) - 4)
        rounded = round(target_price / magnitude) * magnitude
    return f"{symbol}:{direction}:{rounded:g}"


def find_watch_candidates(
    results: "list[ScanResult]",
    *,
    dispatch_threshold: int = 4,
    distance_min: float = WATCH_DISTANCE_MIN,
    distance_max: float = WATCH_DISTANCE_MAX,
) -> list[WatchCandidate]:
    """Extract approaching-setup candidates from a list of scan results.

    Selection:
      - has a setup + evaluation (not error/skipped)
      - total_score < dispatch_threshold (otherwise it's dispatched as confirmed)
      - layer_2.detail has closest_price + distance_pct
      - distance_pct is within [distance_min, distance_max]
    """
    out: list[WatchCandidate] = []
    for r in results:
        if r.setup is None or r.evaluation is None:
            continue
        if r.evaluation.total_score >= dispatch_threshold:
            continue
        l2_detail = r.evaluation.layer_2.detail
        dist = l2_detail.get("distance_pct")
        target = l2_detail.get("closest_price")
        if dist is None or target is None or target <= 0:
            continue
        if not (distance_min <= float(dist) <= distance_max):
            continue
        out.append(
            WatchCandidate(
                symbol=r.symbol,
                direction=r.setup.direction,
                target_price=float(target),
                target_label=str(l2_detail.get("closest_label", "level")),
                current_entry=r.setup.entry,
                distance_pct=float(dist),
            )
        )
    return out


def format_watch_message(c: WatchCandidate) -> str:
    delta = c.target_price - c.current_entry
    needed_pct = delta / c.current_entry * 100
    direction_word = "내려와야" if needed_pct < 0 else "올라가야"
    return (
        f"⏳ {c.symbol} {c.direction.upper()} watchlist\n"
        f"   현재 ~{c.current_entry:,.2f} → 목표 {c.target_label} @ {c.target_price:,.2f}\n"
        f"   {abs(needed_pct):.2f}% {direction_word} setup 성립 (Layer 2)"
    )


def filter_unalerted(
    candidates: list[WatchCandidate], state: WatchlistState,
) -> list[WatchCandidate]:
    """Return only candidates whose idempotency key isn't in state yet."""
    fresh: list[WatchCandidate] = []
    for c in candidates:
        if state.already_alerted(watch_key(c.symbol, c.direction, c.target_price)):
            continue
        fresh.append(c)
    return fresh


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
