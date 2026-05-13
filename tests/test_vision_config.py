"""Unit tests for agents.vision_config toggle module (Phase 8c PR-1)."""
from __future__ import annotations

import pytest

from agents.vision_config import (
    vision_advisor_enabled,
    vision_check_enabled,
    vision_globally_disabled,
    vision_override_is_dry_run,
)

_TOGGLES = (
    "VISION_DISABLED",
    "VISION_ADVISOR_DISABLED",
    "VISION_CHECK_DISABLED",
    "VISION_OVERRIDE_DRY_RUN",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for env in _TOGGLES:
        monkeypatch.delenv(env, raising=False)


def test_defaults_are_enabled():
    assert vision_globally_disabled() is False
    assert vision_advisor_enabled() is True
    assert vision_check_enabled() is True
    assert vision_override_is_dry_run() is False


def test_global_disable_overrides_individual_toggles(monkeypatch):
    monkeypatch.setenv("VISION_DISABLED", "1")
    assert vision_globally_disabled() is True
    assert vision_advisor_enabled() is False
    assert vision_check_enabled() is False


def test_advisor_disable_does_not_affect_check(monkeypatch):
    monkeypatch.setenv("VISION_ADVISOR_DISABLED", "1")
    assert vision_advisor_enabled() is False
    assert vision_check_enabled() is True  # independent toggle


def test_check_disable_does_not_affect_advisor(monkeypatch):
    monkeypatch.setenv("VISION_CHECK_DISABLED", "1")
    assert vision_check_enabled() is False
    assert vision_advisor_enabled() is True


def test_dry_run_toggle(monkeypatch):
    monkeypatch.setenv("VISION_OVERRIDE_DRY_RUN", "true")
    assert vision_override_is_dry_run() is True


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "FALSE"])
def test_falsy_values_do_not_enable_disable_flag(monkeypatch, falsy):
    monkeypatch.setenv("VISION_DISABLED", falsy)
    assert vision_globally_disabled() is False
