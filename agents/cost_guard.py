"""Daily cost + rate-limit guard for the vision tier (Phase 8c).

The vision tier sends ~250 KB chart PNGs to Sonnet 4.6, which costs ~3¢
per call at current pricing — affordable per call, ruinous if the scanner
runs in a tight loop. ``CostGuard`` enforces two ceilings:

  1. Daily $ cap   — env ``MAX_DAILY_VISION_COST_USD`` (default $5/day)
  2. Hourly count  — env ``VISION_RATE_LIMIT_PER_HOUR`` (default 30 calls)

Persistence is a single jsonl file (default ``.tv-vision-costs.jsonl``
in the repo root), one line per call:

    {"ts": "...", "model": "...", "input": ..., "output": ...,
     "cache_read": ..., "cache_write": ..., "cost_usd": ...}

Inspection / reset for the operator is just ``cat`` / ``rm``.

Concurrency: a single-process, scanner-style writer is the only expected
caller. We append-write under a lock per-instance; cross-process safety
is out of scope (each call is one short append and reads tolerate noise).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

# Pricing table — USD per million tokens. Update when Anthropic changes
# their list price. Kept here (not in client) so cost math has a single
# source of truth.
PRICING_PER_MTOK: Final[dict[str, dict[str, float]]] = {
    "claude-opus-4-7":      {"in": 15.0, "out": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6":    {"in":  3.0, "out": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-5":     {"in":  1.0, "out":  5.0, "cache_read": 0.10, "cache_write":  1.25},
    # The dated Haiku ID returned by the API for the same model.
    "claude-haiku-4-5-20251001": {
        "in": 1.0, "out": 5.0, "cache_read": 0.10, "cache_write": 1.25,
    },
}

DEFAULT_MAX_DAILY_USD: Final = 5.0
DEFAULT_RATE_LIMIT_PER_HOUR: Final = 30
DEFAULT_LOG_PATH: Final = Path(".tv-vision-costs.jsonl")

MAX_DAILY_ENV: Final = "MAX_DAILY_VISION_COST_USD"
RATE_LIMIT_ENV: Final = "VISION_RATE_LIMIT_PER_HOUR"
LOG_PATH_ENV: Final = "VISION_COST_LOG_PATH"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return USD cost from token counts using ``PRICING_PER_MTOK``.

    Unknown models default to Sonnet pricing (a safe over-estimate for
    Haiku, an under-estimate for Opus — log a warning at the caller if
    that distinction matters).
    """
    rates = PRICING_PER_MTOK.get(model) or PRICING_PER_MTOK["claude-sonnet-4-6"]
    return (
        input_tokens       * rates["in"]          / 1_000_000
        + output_tokens    * rates["out"]         / 1_000_000
        + cache_read_tokens  * rates["cache_read"]  / 1_000_000
        + cache_write_tokens * rates["cache_write"] / 1_000_000
    )


@dataclass
class CostGuard:
    """Daily $ cap + hourly call-count rate limit, persisted to jsonl."""

    log_path: Path = DEFAULT_LOG_PATH
    max_daily_usd: float = DEFAULT_MAX_DAILY_USD
    rate_limit_per_hour: int = DEFAULT_RATE_LIMIT_PER_HOUR
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ---- public surface --------------------------------------------------

    def allow(self) -> bool:
        """True iff a new call would stay under both daily $ and hourly count caps."""
        records = self._load()
        now = _now()
        spent_today = sum(
            r.get("cost_usd", 0.0) for r in records if _is_same_utc_day(r, now)
        )
        if spent_today >= self.max_daily_usd:
            return False
        one_hour_ago = now - timedelta(hours=1)
        recent = sum(
            1 for r in records if _is_after(r, one_hour_ago)
        )
        if recent >= self.rate_limit_per_hour:
            return False
        return True

    def record(self, usage: Any, *, model: str) -> None:
        """Append one usage record. ``usage`` may be an Anthropic SDK
        ``Usage`` object or a plain dict with the same field names.

        Silent on errors — cost telemetry must never break the call path.
        """
        try:
            input_tokens       = int(_field(usage, "input_tokens", 0))
            output_tokens      = int(_field(usage, "output_tokens", 0))
            cache_read_tokens  = int(_field(usage, "cache_read_input_tokens", 0) or 0)
            cache_write_tokens = int(_field(usage, "cache_creation_input_tokens", 0) or 0)
        except (TypeError, ValueError):
            return
        cost = estimate_cost_usd(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        entry = {
            "ts": _now().isoformat(),
            "model": model,
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read_tokens,
            "cache_write": cache_write_tokens,
            "cost_usd": round(cost, 6),
        }
        self._append(entry)

    def daily_spend_usd(self, *, at: datetime | None = None) -> float:
        """Total $ spent for the UTC day containing ``at`` (default now)."""
        records = self._load()
        ref = at or _now()
        return round(
            sum(r.get("cost_usd", 0.0) for r in records if _is_same_utc_day(r, ref)),
            6,
        )

    def calls_last_hour(self, *, at: datetime | None = None) -> int:
        records = self._load()
        ref = at or _now()
        cutoff = ref - timedelta(hours=1)
        return sum(1 for r in records if _is_after(r, cutoff))

    # ---- persistence helpers --------------------------------------------

    def _append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError:
                # Cost logging failure must never break the actual call.
                return

    def _load(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with self.log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return out


# --- helpers --------------------------------------------------------------


def _field(obj: Any, name: str, default: Any) -> Any:
    """Read attribute (SDK Usage object) or key (dict). None counts as default."""
    if isinstance(obj, dict):
        val = obj.get(name, default)
    else:
        val = getattr(obj, name, default)
    return default if val is None else val


def _parse_ts(record: dict[str, Any]) -> datetime | None:
    ts = record.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _is_same_utc_day(record: dict[str, Any], ref: datetime) -> bool:
    ts = _parse_ts(record)
    if ts is None:
        return False
    return ts.astimezone(timezone.utc).date() == ref.astimezone(timezone.utc).date()


def _is_after(record: dict[str, Any], cutoff: datetime) -> bool:
    ts = _parse_ts(record)
    if ts is None:
        return False
    return ts >= cutoff


# --- factory --------------------------------------------------------------


def from_env() -> CostGuard:
    """Build a ``CostGuard`` from the relevant env vars (with safe defaults)."""

    def _f(env: str, default: float) -> float:
        raw = os.environ.get(env, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def _i(env: str, default: int) -> int:
        raw = os.environ.get(env, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    path_raw = os.environ.get(LOG_PATH_ENV, "").strip()
    log_path = Path(path_raw) if path_raw else DEFAULT_LOG_PATH
    return CostGuard(
        log_path=log_path,
        max_daily_usd=_f(MAX_DAILY_ENV, DEFAULT_MAX_DAILY_USD),
        rate_limit_per_hour=_i(RATE_LIMIT_ENV, DEFAULT_RATE_LIMIT_PER_HOUR),
    )
