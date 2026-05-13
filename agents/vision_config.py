"""Single source of truth for vision-tier feature toggles (Phase 8c).

All env-var names live here so PR-2 / PR-3 callers (position advisor +
pre-entry check) import them rather than open-coding strings.

Toggles:
    VISION_DISABLED            — hard kill switch for the whole vision tier
    VISION_ADVISOR_DISABLED    — disable vision in the position advisor only
    VISION_CHECK_DISABLED      — disable the pre-entry vision check only
    VISION_OVERRIDE_DRY_RUN    — vision-recommended overrides are LOGGED but
                                 not enforced. Useful for ramping up trust
                                 in the tier without giving it veto power.

Cost guard toggles live in ``agents.cost_guard``:
    MAX_DAILY_VISION_COST_USD
    VISION_RATE_LIMIT_PER_HOUR
"""
from __future__ import annotations

import os
from typing import Final

VISION_DISABLED_ENV: Final = "VISION_DISABLED"
VISION_ADVISOR_DISABLED_ENV: Final = "VISION_ADVISOR_DISABLED"
VISION_CHECK_DISABLED_ENV: Final = "VISION_CHECK_DISABLED"
VISION_OVERRIDE_DRY_RUN_ENV: Final = "VISION_OVERRIDE_DRY_RUN"

_TRUTHY: Final = frozenset({"1", "true", "True", "TRUE", "yes", "on"})


def _flag(env: str) -> bool:
    return os.environ.get(env, "").strip() in _TRUTHY


def vision_globally_disabled() -> bool:
    return _flag(VISION_DISABLED_ENV)


def vision_advisor_enabled() -> bool:
    """True iff vision should run for the open-position advisor."""
    return not (vision_globally_disabled() or _flag(VISION_ADVISOR_DISABLED_ENV))


def vision_check_enabled() -> bool:
    """True iff the pre-entry vision check should run."""
    return not (vision_globally_disabled() or _flag(VISION_CHECK_DISABLED_ENV))


def vision_override_is_dry_run() -> bool:
    """True iff vision verdicts should be logged but not enforced."""
    return _flag(VISION_OVERRIDE_DRY_RUN_ENV)
