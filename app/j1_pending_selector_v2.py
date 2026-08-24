from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app import daily_prediction_runner as legacy
from app.database import SessionLocal
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.league_registry import canonical_league
from app.models import Fixture

J1_PENDING_SELECTOR_VERSION = "j1_pending_fixture_selector_v2"

_installed = False
_original_due_target_fixtures = legacy._due_target_fixtures
_last_audit: dict[str, Any] | None = None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_due_candidates(*, now: datetime, max_lateness_minutes: int) -> list[Fixture]:
    latest_kickoff = now + timedelta(minutes=legacy.J1_TARGET_LEAD_MINUTES)
    earliest_kickoff = now + timedelta(
        minutes=max(1, legacy.J1_TARGET_LEAD_MINUTES - max_lateness_minutes)
    )

    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Fixture)
                .where(
                    Fixture.starts_at >= earliest_kickoff,
                    Fixture.starts_at <= latest_kickoff,
                    Fixture.starts_at > now,
                )
                .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
            ).all()
        )


def _target_candidates(candidates: list[Fixture]) -> list[Fixture]:
    target: list[Fixture] = []
    for fixture in candidates:
        canonical = canonical_league(fixture.league_name)
        if canonical.get("target") and canonical.get("key"):
            target.append(fixture)
    return target


def _recorded_fixture_windows(fixtures: list[Fixture]) -> set[tuple[int, str]]:
    if not fixtures:
        return set()

    ensure_forward_test_schema()
    fixture_ids = [int(fixture.id) for fixture in fixtures]
    with SessionLocal() as session:
        rows = session.execute(
            select(DecisionRecord.fixture_id, DecisionRecord.snapshot_window).where(
                DecisionRecord.fixture_id.in_(fixture_ids),
                DecisionRecord.source == legacy.DAILY_PREDICTION_RUNNER_VERSION,
            )
        ).all()
    return {
        (int(fixture_id), str(snapshot_window))
        for fixture_id, snapshot_window in rows
    }


def select_pending_j1_fixtures(
    *,
    now: datetime,
    max_lateness_minutes: int,
    max_fixtures: int,
) -> tuple[list[Fixture], dict[str, Any]]:
    """Select J1 work after excluding immutable ledger records.

    V1 applied ``max_fixtures`` before checking the ledger. Once the first batch
    had been recorded, those same fixtures could keep occupying every slot and
    starve later fixtures with the same J1 window. V2 deliberately loads the
    whole bounded due window, filters target leagues, excludes already-recorded
    fixture/window pairs, and only then applies the per-cycle safety limit.
    """

    if max_lateness_minutes < 1 or max_lateness_minutes > 30:
        raise ValueError("max_lateness_minutes must be between 1 and 30")
    if max_fixtures < 1:
        raise ValueError("max_fixtures must be at least 1")

    now = _aware_utc(now)
    candidates = _load_due_candidates(
        now=now,
        max_lateness_minutes=max_lateness_minutes,
    )
    target = _target_candidates(candidates)
    recorded = _recorded_fixture_windows(target)

    pending = [
        fixture
        for fixture in target
        if (int(fixture.id), legacy._snapshot_window(fixture)) not in recorded
    ]
    selected = pending[:max_fixtures]

    audit = {
        "version": J1_PENDING_SELECTOR_VERSION,
        "due_candidate_count": len(candidates),
        "target_candidate_count": len(target),
        "already_recorded_excluded": len(target) - len(pending),
        "pending_before_limit": len(pending),
        "selected_fixture_count": len(selected),
        "deferred_pending_fixture_count": max(0, len(pending) - len(selected)),
        "max_fixtures": max_fixtures,
        "selection_limit_applied_after_recorded_exclusion": True,
        "recorded_fixture_windows_do_not_consume_batch_capacity": True,
    }
    return selected, audit


def _due_target_fixtures_v2(
    *,
    now: datetime,
    max_lateness_minutes: int,
    max_fixtures: int,
) -> list[Fixture]:
    global _last_audit
    selected, audit = select_pending_j1_fixtures(
        now=now,
        max_lateness_minutes=max_lateness_minutes,
        max_fixtures=max_fixtures,
    )
    _last_audit = audit
    return selected


def install_j1_pending_selector_v2() -> None:
    """Install V2 behind the legacy selector interface used by Runner V2."""

    global _installed
    if _installed:
        return
    legacy._due_target_fixtures = _due_target_fixtures_v2
    _installed = True


def last_j1_pending_selector_audit() -> dict[str, Any] | None:
    return dict(_last_audit) if _last_audit is not None else None


def restore_legacy_j1_selector_for_tests() -> None:
    """Test-only escape hatch; production code should never call this."""

    global _installed, _last_audit
    legacy._due_target_fixtures = _original_due_target_fixtures
    _installed = False
    _last_audit = None
