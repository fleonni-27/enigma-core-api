from __future__ import annotations

import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Iterator

from sqlalchemy import event, select

from app.database import SessionLocal
from app.models import Fixture, Prediction

PREDICTION_WINDOW_POLICY_VERSION = "prediction_window_policy_v1"
J1_RESERVED_WINDOW = "j1_45m_v1"
J1_RESERVED_OWNER = "daily_prediction_runner_v2"
RESERVED_PREDICTION_WINDOWS = {
    J1_RESERVED_WINDOW: J1_RESERVED_OWNER,
}

_active_prediction_producer: ContextVar[str | None] = ContextVar(
    "active_prediction_producer",
    default=None,
)
_installed = False
_endpoint_guard_installed = False


class ReservedPredictionWindowError(ValueError):
    pass


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reserved_window_owner(prediction_window: str) -> str | None:
    return RESERVED_PREDICTION_WINDOWS.get(str(prediction_window or "").strip())


def assert_prediction_window_write_allowed(prediction_window: str) -> None:
    window = str(prediction_window or "").strip()
    owner = reserved_window_owner(window)
    if owner is None:
        return
    producer = _active_prediction_producer.get()
    if producer != owner:
        raise ReservedPredictionWindowError(
            f"prediction_window '{window}' is reserved for {owner}"
        )


@contextmanager
def authorized_prediction_producer(producer: str) -> Iterator[None]:
    token = _active_prediction_producer.set(str(producer))
    try:
        yield
    finally:
        _active_prediction_producer.reset(token)


def _guard_prediction_write(mapper, connection, target: Prediction) -> None:
    assert_prediction_window_write_allowed(target.prediction_window)


def _install_fast_http_guard() -> None:
    """Patch the already-imported legacy inference function used by the public route.

    The database mapper guard below is the integrity boundary. This wrapper only
    rejects reserved-window requests before the expensive inference fit starts.
    """

    global _endpoint_guard_installed
    if _endpoint_guard_installed:
        return

    main_module = sys.modules.get("app.main")
    if main_module is None:
        return
    original = getattr(main_module, "generate_and_persist_prematch_prediction", None)
    if original is None:
        return
    if getattr(original, "_prediction_window_guarded", False):
        _endpoint_guard_installed = True
        return

    @wraps(original)
    def guarded_generate(*args, **kwargs):
        prediction_window = kwargs.get("prediction_window", "prematch_v1")
        assert_prediction_window_write_allowed(str(prediction_window))
        return original(*args, **kwargs)

    guarded_generate._prediction_window_guarded = True  # type: ignore[attr-defined]
    main_module.generate_and_persist_prematch_prediction = guarded_generate
    _endpoint_guard_installed = True


def install_prediction_window_policy() -> None:
    global _installed
    if not _installed:
        event.listen(Prediction, "before_insert", _guard_prediction_write, propagate=True)
        event.listen(Prediction, "before_update", _guard_prediction_write, propagate=True)
        _installed = True
    _install_fast_http_guard()


def quarantine_invalid_reserved_j1_predictions(
    *,
    now: datetime,
    prediction_window: str,
    model_version: str,
    target_lead_minutes: int,
    max_lateness_minutes: int,
) -> dict[str, Any]:
    """Preserve but move invalid legacy J1 rows out of the reserved unique key.

    Before this policy existed, a manual early write could occupy the immutable
    `(fixture_id, prediction_window, model_version)` key. The operational J1
    scheduler is allowed to quarantine only rows whose generated_at falls outside
    the valid J1 interval. Probabilities are never overwritten or deleted.
    """

    now = _aware_utc(now)
    latest_kickoff = now + timedelta(minutes=target_lead_minutes)
    earliest_kickoff = now + timedelta(
        minutes=max(1, target_lead_minutes - max_lateness_minutes)
    )

    with SessionLocal() as session:
        rows = session.execute(
            select(Prediction, Fixture)
            .join(Fixture, Prediction.fixture_id == Fixture.id)
            .where(
                Prediction.prediction_window == prediction_window,
                Prediction.model_version == model_version,
                Fixture.starts_at >= earliest_kickoff,
                Fixture.starts_at <= latest_kickoff,
                Fixture.starts_at > now,
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc(), Prediction.id.asc())
        ).all()

        quarantined: list[dict[str, Any]] = []
        for prediction, fixture in rows:
            starts_at = _aware_utc(fixture.starts_at)
            due_at = starts_at - timedelta(minutes=target_lead_minutes)
            generated_at = _aware_utc(prediction.generated_at)
            if due_at <= generated_at < starts_at:
                continue

            original_window = prediction.prediction_window
            quarantine_window = f"invalid_j1_{int(prediction.id)}"[:30]
            prediction.prediction_window = quarantine_window
            quarantined.append(
                {
                    "prediction_id": int(prediction.id),
                    "fixture_id": int(fixture.id),
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "original_window": original_window,
                    "quarantine_window": quarantine_window,
                    "generated_at": generated_at.isoformat(),
                    "j1_due_at": due_at.isoformat(),
                    "kickoff_at": starts_at.isoformat(),
                    "reason": (
                        "GENERATED_BEFORE_J1_DUE"
                        if generated_at < due_at
                        else "GENERATED_AT_OR_AFTER_KICKOFF"
                    ),
                }
            )

        if quarantined:
            session.commit()

    return {
        "version": PREDICTION_WINDOW_POLICY_VERSION,
        "reserved_window": prediction_window,
        "reserved_owner": reserved_window_owner(prediction_window),
        "rows_scanned": len(rows),
        "quarantined_count": len(quarantined),
        "quarantined": quarantined[:20],
        "policy": {
            "manual_reserved_window_writes_blocked": True,
            "invalid_legacy_rows_deleted": False,
            "invalid_legacy_probabilities_overwritten": False,
            "invalid_legacy_rows_preserved_under_quarantine_window": True,
            "valid_j1_predictions_remain_immutable": True,
        },
    }
