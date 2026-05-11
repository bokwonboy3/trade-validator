"""Tests for SpecialistCache — TTL semantics, swing-price tolerance, JSON roundtrip."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agents.specialist_cache import (
    CacheEntry,
    DEFAULT_CACHE_PATH,
    SpecialistCache,
)
from agents.types import AgentVerdict, SpecialistOutput
from alert_state import ALERT_TTL_HOURS


def _verdict() -> AgentVerdict:
    return AgentVerdict(
        verdict="ENTER",
        confidence=80,
        rationale="strong setup",
        tier1_verdict="ENTER",
        downgraded_from_tier1=False,
    )


def _specialists() -> list[SpecialistOutput]:
    return [
        SpecialistOutput(
            name="microstructure",
            findings={"signal": "bullish_rejection"},
            confidence=8,
            rationale="clear rejection at level",
        ),
        SpecialistOutput(
            name="macro",
            failed=True,
            failure_reason="funding rate unavailable",
        ),
    ]


def test_load_missing_file_returns_empty(tmp_path):
    p = tmp_path / "cache.json"
    c = SpecialistCache.load(p)
    assert c.entries == []
    assert c.path == p


def test_put_get_roundtrip(tmp_path):
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())

    hit = c.get("BTCUSDT", "long", 80_000.0, "pass")
    assert hit is not None
    v, specs = hit
    assert v.verdict == "ENTER"
    assert v.confidence == 80
    assert len(specs) == 2
    assert specs[0].name == "microstructure"
    assert specs[0].findings == {"signal": "bullish_rejection"}
    assert specs[1].failed is True


def test_get_miss_for_different_symbol(tmp_path):
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())
    assert c.get("ETHUSDT", "long", 80_000.0, "pass") is None


def test_get_miss_for_different_direction(tmp_path):
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())
    assert c.get("BTCUSDT", "short", 80_000.0, "pass") is None


def test_get_miss_for_different_layer_3_status(tmp_path):
    """FORMING-context verdict must not be served to a CONFIRMED-context lookup.

    The microstructure specialist receives layer_3.status in its prompt, so a
    rationale produced under L3=fail is stale once L3=pass.
    """
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "fail", _verdict(), _specialists())
    assert c.get("BTCUSDT", "long", 80_000.0, "pass") is None
    assert c.get("BTCUSDT", "long", 80_000.0, "pending") is None
    assert c.get("BTCUSDT", "long", 80_000.0, "fail") is not None


def test_get_hit_within_swing_price_tolerance(tmp_path):
    """0.05% off still resolves to the same cached entry."""
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())
    # 0.05% off — within 0.1% tolerance
    assert c.get("BTCUSDT", "long", 80_040.0, "pass") is not None


def test_get_miss_outside_swing_price_tolerance(tmp_path):
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())
    # 0.5% off — outside tolerance
    assert c.get("BTCUSDT", "long", 80_400.0, "pass") is None


def test_put_replaces_existing_entry_for_same_key(tmp_path):
    c = SpecialistCache(path=tmp_path / "cache.json")
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())

    new_verdict = AgentVerdict(
        verdict="WATCH", confidence=40, rationale="weaker now",
        tier1_verdict="ENTER", downgraded_from_tier1=True,
    )
    c.put("BTCUSDT", "long", 80_010.0, "pass", new_verdict, [])  # within tolerance — same key

    assert len(c.entries) == 1
    hit = c.get("BTCUSDT", "long", 80_000.0, "pass")
    assert hit is not None
    assert hit[0].verdict == "WATCH"
    assert hit[0].downgraded_from_tier1 is True


def test_put_keeps_separate_entries_per_layer_3_status(tmp_path):
    """Same (symbol, direction, swing) under different L3 status → two entries."""
    c = SpecialistCache(path=tmp_path / "cache.json")
    forming_v = AgentVerdict(
        verdict="WATCH", confidence=50, rationale="forming",
        tier1_verdict="WATCH", downgraded_from_tier1=False,
    )
    confirmed_v = AgentVerdict(
        verdict="ENTER", confidence=85, rationale="confirmed",
        tier1_verdict="ENTER", downgraded_from_tier1=False,
    )
    c.put("BTCUSDT", "long", 80_000.0, "fail", forming_v, [])
    c.put("BTCUSDT", "long", 80_000.0, "pass", confirmed_v, [])

    assert len(c.entries) == 2
    assert c.get("BTCUSDT", "long", 80_000.0, "fail")[0].verdict == "WATCH"
    assert c.get("BTCUSDT", "long", 80_000.0, "pass")[0].verdict == "ENTER"


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "cache.json"
    c1 = SpecialistCache(path=p)
    c1.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())
    c1.save()

    c2 = SpecialistCache.load(p)
    assert len(c2.entries) == 1
    hit = c2.get("BTCUSDT", "long", 80_000.0, "pass")
    assert hit is not None
    v, specs = hit
    assert v.verdict == "ENTER"
    assert specs[1].failed is True
    assert specs[1].failure_reason == "funding rate unavailable"


def test_save_uses_atomic_write(tmp_path):
    p = tmp_path / "cache.json"
    c = SpecialistCache(path=p)
    c.put("BTCUSDT", "long", 80_000.0, "pass", _verdict(), _specialists())
    c.save()
    assert p.exists()
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_stale_entries_pruned_on_load(tmp_path):
    p = tmp_path / "cache.json"
    stale = datetime.now(timezone.utc) - timedelta(hours=ALERT_TTL_HOURS + 1)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    body = {
        "entries": [
            {
                "symbol": "OLD", "direction": "long", "sl_swing_price": 100.0,
                "layer_3_status": "pass",
                "cached_at": stale.isoformat(),
                "verdict": {
                    "verdict": "ENTER", "confidence": 80, "rationale": "x",
                    "tier1_verdict": "ENTER", "downgraded_from_tier1": False,
                },
                "specialists": [],
            },
            {
                "symbol": "NEW", "direction": "long", "sl_swing_price": 100.0,
                "layer_3_status": "pass",
                "cached_at": fresh.isoformat(),
                "verdict": {
                    "verdict": "ENTER", "confidence": 80, "rationale": "x",
                    "tier1_verdict": "ENTER", "downgraded_from_tier1": False,
                },
                "specialists": [],
            },
        ]
    }
    p.write_text(json.dumps(body))
    c = SpecialistCache.load(p)
    assert len(c.entries) == 1
    assert c.entries[0].symbol == "NEW"


def test_load_drops_pre_schema_entries_without_layer_3_status(tmp_path):
    """Entries from before the schema bump have no layer_3_status — we can't
    safely classify them, so load() drops them rather than risk serving stale
    forming-context verdicts as confirmed."""
    p = tmp_path / "cache.json"
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    body = {
        "entries": [
            {
                "symbol": "LEGACY", "direction": "long", "sl_swing_price": 100.0,
                # NB: no layer_3_status field
                "cached_at": fresh.isoformat(),
                "verdict": {
                    "verdict": "ENTER", "confidence": 80, "rationale": "x",
                    "tier1_verdict": "ENTER", "downgraded_from_tier1": False,
                },
                "specialists": [],
            },
        ]
    }
    p.write_text(json.dumps(body))
    c = SpecialistCache.load(p)
    assert c.entries == []


def test_get_outside_ttl_returns_none(tmp_path):
    """Even when an entry survives load(), a get() with a tighter TTL skips it."""
    p = tmp_path / "cache.json"
    c = SpecialistCache(path=p)
    c.entries.append(
        CacheEntry(
            symbol="BTCUSDT",
            direction="long",
            sl_swing_price=80_000.0,
            layer_3_status="pass",
            cached_at=(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
            verdict={
                "verdict": "ENTER", "confidence": 80, "rationale": "x",
                "tier1_verdict": "ENTER", "downgraded_from_tier1": False,
            },
            specialists=[],
        )
    )
    # Cache holds it (under default 24h), but a 1h ttl arg rejects it.
    assert c.get("BTCUSDT", "long", 80_000.0, "pass") is not None
    assert c.get("BTCUSDT", "long", 80_000.0, "pass", ttl_hours=1) is None


def test_load_corrupted_file_returns_empty(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text("not valid json {")
    c = SpecialistCache.load(p)
    assert c.entries == []


def test_default_path_constant():
    """Sanity — default lives next to the alert state file."""
    assert str(DEFAULT_CACHE_PATH).endswith("agent-cache.json")
