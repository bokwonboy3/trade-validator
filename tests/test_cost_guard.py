"""Unit tests for agents.cost_guard (Phase 8c PR-1)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents import cost_guard
from agents.cost_guard import (
    CostGuard,
    DEFAULT_MAX_DAILY_USD,
    DEFAULT_RATE_LIMIT_PER_HOUR,
    estimate_cost_usd,
    from_env,
)


# --- pricing math ---------------------------------------------------------


def test_estimate_cost_uses_model_specific_rate():
    """Opus is ~5× Sonnet on input; the math must reflect that."""
    sonnet = estimate_cost_usd(
        model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0,
    )
    opus = estimate_cost_usd(
        model="claude-opus-4-7", input_tokens=1_000_000, output_tokens=0,
    )
    assert sonnet == pytest.approx(3.0)
    assert opus == pytest.approx(15.0)


def test_estimate_cost_unknown_model_falls_back_to_sonnet():
    val = estimate_cost_usd(
        model="not-a-real-model", input_tokens=1_000_000, output_tokens=0,
    )
    assert val == pytest.approx(3.0)


def test_estimate_cost_includes_cache_components():
    val = estimate_cost_usd(
        model="claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    # 0.30 (read) + 3.75 (write) = 4.05
    assert val == pytest.approx(4.05)


# --- record() + persistence -----------------------------------------------


class _FakeUsage:
    def __init__(self, **kw) -> None:
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)


def test_record_appends_jsonl(tmp_path: Path):
    log = tmp_path / "vision-costs.jsonl"
    g = CostGuard(log_path=log)
    g.record(_FakeUsage(input_tokens=100, output_tokens=50), model="claude-sonnet-4-6")
    g.record(_FakeUsage(input_tokens=200, output_tokens=10), model="claude-sonnet-4-6")
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["input"] == 100
    assert rec["output"] == 50
    assert rec["cost_usd"] > 0


def test_record_tolerates_dict_usage(tmp_path: Path):
    """Some test mocks pass a plain dict — must still work."""
    log = tmp_path / "vision-costs.jsonl"
    g = CostGuard(log_path=log)
    g.record(
        {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
        model="claude-sonnet-4-6",
    )
    assert log.exists()
    assert json.loads(log.read_text().strip())["input"] == 10


def test_record_handles_none_cache_fields(tmp_path: Path):
    """Anthropic returns ``None`` for cache fields when no cache hit —
    that must be coerced to 0, not crash."""
    log = tmp_path / "vision-costs.jsonl"
    g = CostGuard(log_path=log)
    g.record(
        _FakeUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        ),
        model="claude-sonnet-4-6",
    )
    rec = json.loads(log.read_text().strip())
    assert rec["cache_read"] == 0 and rec["cache_write"] == 0


# --- allow() — daily cap --------------------------------------------------


def _write_entry(
    log: Path,
    *,
    cost: float,
    ts: datetime,
    model: str = "claude-sonnet-4-6",
) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": ts.isoformat(),
                    "model": model,
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cost_usd": cost,
                }
            )
            + "\n"
        )


def test_allow_blocks_when_daily_cap_reached(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    now = datetime.now(timezone.utc)
    _write_entry(log, cost=4.5, ts=now)
    g = CostGuard(log_path=log, max_daily_usd=4.0, rate_limit_per_hour=100)
    assert g.allow() is False


def test_allow_permits_when_below_cap(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    now = datetime.now(timezone.utc)
    _write_entry(log, cost=1.0, ts=now)
    g = CostGuard(log_path=log, max_daily_usd=4.0, rate_limit_per_hour=100)
    assert g.allow() is True


def test_allow_ignores_prior_day_spend(tmp_path: Path):
    """A maxed-out yesterday must NOT block today's first call."""
    log = tmp_path / "log.jsonl"
    yesterday = datetime.now(timezone.utc) - timedelta(days=2)
    _write_entry(log, cost=100.0, ts=yesterday)
    g = CostGuard(log_path=log, max_daily_usd=4.0, rate_limit_per_hour=100)
    assert g.allow() is True


# --- allow() — hourly rate limit ------------------------------------------


def test_allow_blocks_when_hourly_rate_exceeded(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    now = datetime.now(timezone.utc)
    for _ in range(5):
        _write_entry(log, cost=0.01, ts=now)
    g = CostGuard(log_path=log, max_daily_usd=100.0, rate_limit_per_hour=5)
    assert g.allow() is False


def test_allow_recovers_after_old_calls_age_out(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    for _ in range(50):
        _write_entry(log, cost=0.01, ts=long_ago)
    g = CostGuard(log_path=log, max_daily_usd=100.0, rate_limit_per_hour=5)
    # 50 calls 2h ago should not block now.
    assert g.allow() is True


# --- introspection --------------------------------------------------------


def test_daily_spend_sums_today_only(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=2)
    _write_entry(log, cost=1.25, ts=today)
    _write_entry(log, cost=0.75, ts=today)
    _write_entry(log, cost=99.0, ts=yesterday)
    g = CostGuard(log_path=log)
    assert g.daily_spend_usd() == pytest.approx(2.0)


def test_calls_last_hour_counts_only_recent(tmp_path: Path):
    log = tmp_path / "log.jsonl"
    now = datetime.now(timezone.utc)
    for _ in range(3):
        _write_entry(log, cost=0.01, ts=now)
    _write_entry(log, cost=0.01, ts=now - timedelta(hours=3))
    g = CostGuard(log_path=log)
    assert g.calls_last_hour() == 3


# --- env-based factory ----------------------------------------------------


def test_from_env_uses_defaults_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("MAX_DAILY_VISION_COST_USD", raising=False)
    monkeypatch.delenv("VISION_RATE_LIMIT_PER_HOUR", raising=False)
    monkeypatch.setenv("VISION_COST_LOG_PATH", str(tmp_path / "log.jsonl"))
    g = from_env()
    assert g.max_daily_usd == DEFAULT_MAX_DAILY_USD
    assert g.rate_limit_per_hour == DEFAULT_RATE_LIMIT_PER_HOUR


def test_from_env_parses_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_DAILY_VISION_COST_USD", "12.50")
    monkeypatch.setenv("VISION_RATE_LIMIT_PER_HOUR", "7")
    monkeypatch.setenv("VISION_COST_LOG_PATH", str(tmp_path / "log.jsonl"))
    g = from_env()
    assert g.max_daily_usd == pytest.approx(12.50)
    assert g.rate_limit_per_hour == 7


def test_from_env_ignores_garbage_values(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_DAILY_VISION_COST_USD", "not-a-number")
    monkeypatch.setenv("VISION_RATE_LIMIT_PER_HOUR", "nope")
    monkeypatch.setenv("VISION_COST_LOG_PATH", str(tmp_path / "log.jsonl"))
    g = from_env()
    assert g.max_daily_usd == DEFAULT_MAX_DAILY_USD
    assert g.rate_limit_per_hour == DEFAULT_RATE_LIMIT_PER_HOUR
