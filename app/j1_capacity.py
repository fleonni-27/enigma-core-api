from __future__ import annotations

import os
from typing import Any

J1_MAX_FIXTURES_ENV = "J1_MAX_FIXTURES"
J1_FIXTURE_STAGES = (5, 10, 20)
DEFAULT_J1_MAX_FIXTURES = 5
HARD_MAX_J1_FIXTURES = 20


def configured_j1_max_fixtures() -> int:
    raw = str(os.getenv(J1_MAX_FIXTURES_ENV, DEFAULT_J1_MAX_FIXTURES)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{J1_MAX_FIXTURES_ENV} must be one of {J1_FIXTURE_STAGES}; got {raw!r}"
        ) from exc
    if value not in J1_FIXTURE_STAGES:
        raise ValueError(
            f"{J1_MAX_FIXTURES_ENV} must be one of {J1_FIXTURE_STAGES}; got {value}"
        )
    return value


def activate_j1_runner_capacity() -> dict[str, Any]:
    """Raise the V2 runner hard ceiling while keeping the operational stage external.

    The underlying runner historically inherited a five-fixture hard cap from the
    legacy implementation. The scheduler owns scale policy now: this function
    raises only that safety ceiling to 20. The active per-cycle limit still comes
    from J1_MAX_FIXTURES (5, 10, or 20) and is always passed explicitly.
    """
    from app import daily_prediction_runner_v2 as runner_module

    previous = int(runner_module.MAX_FIXTURES_PER_RUN)
    runner_module.MAX_FIXTURES_PER_RUN = HARD_MAX_J1_FIXTURES
    return {
        "policy": "staged_5_10_20",
        "environment_variable": J1_MAX_FIXTURES_ENV,
        "configured_max_fixtures": configured_j1_max_fixtures(),
        "hard_max_fixtures": HARD_MAX_J1_FIXTURES,
        "previous_runner_hard_max": previous,
        "stages": list(J1_FIXTURE_STAGES),
    }
