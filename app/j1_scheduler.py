from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine
from app.daily_prediction_runner_v2 import (
    DAILY_PREDICTION_RUNNER_VERSION,
    DEFAULT_MAX_FIXTURES,
    DEFAULT_MAX_LATENESS_MINUTES,
    J1_PREDICTION_WINDOW,
    J1_TARGET_LEAD_MINUTES,
    run_daily_prediction_runner,
)
from app.j1_capacity import activate_j1_runner_capacity, configured_j1_max_fixtures
from app.j1_pending_selector_v2 import (
    J1_PENDING_SELECTOR_VERSION,
    install_j1_pending_selector_v2,
)
from app.performance_observatory import (
    PIPELINE_J1,
    record_j1_result,
    try_persist_pipeline_sample,
)
from app.prematch_inference import MODEL_VERSION
from app.prediction_window_policy import (
    PREDICTION_WINDOW_POLICY_VERSION,
    authorized_prediction_producer,
    install_prediction_window_policy,
    quarantine_invalid_reserved_j1_predictions,
)

J1_SCHEDULER_VERSION = "j1_scheduler_v2"
J1_OPERATION_NAME = "DAILY_PREDICTION_J1"
J1_ADVISORY_LOCK_KEY = 450026
J1_HEARTBEAT_STALE_MINUTES = 20

_schema_lock = Lock()
_schema_ready = False


class OperationRunRecord(Base):
    __tablename__ = "operation_run_records"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selected_fixtures: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


def ensure_operation_run_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        OperationRunRecord.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def _start_run(source: str) -> int:
    ensure_operation_run_schema()
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        row = OperationRunRecord(
            operation=J1_OPERATION_NAME,
            source=source,
            status="RUNNING",
            started_at=now,
            selected_fixtures=0,
            counts={},
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


def _finish_run(
    run_id: int,
    *,
    status: str,
    selected_fixtures: int = 0,
    counts: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    ensure_operation_run_schema()
    with SessionLocal() as session:
        row = session.get(OperationRunRecord, run_id)
        if row is None:
            return
        row.status = status
        row.finished_at = datetime.now(timezone.utc)
        row.selected_fixtures = int(selected_fixtures)
        row.counts = dict(counts or {})
        row.error = error
        session.commit()


def latest_j1_run() -> OperationRunRecord | None:
    ensure_operation_run_schema()
    with SessionLocal() as session:
        row = session.scalar(
            select(OperationRunRecord)
            .where(OperationRunRecord.operation == J1_OPERATION_NAME)
            .order_by(OperationRunRecord.started_at.desc(), OperationRunRecord.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        session.expunge(row)
        return row


def _scheduler_status_from_result(result: dict[str, Any]) -> tuple[str, str | None]:
    if result.get("status") != "ok":
        return "FAILED", str(result.get("status") or "NON_OK_RESULT")[:160]

    health = str((result.get("run_health") or {}).get("status") or "UNKNOWN")
    if health in {"OK", "IDLE"}:
        return health, None
    if health == "DEGRADED":
        reasons = ",".join((result.get("run_health") or {}).get("reason_codes") or [])
        return "DEGRADED", reasons[:160] or None
    if health == "FAILED":
        reasons = ",".join((result.get("run_health") or {}).get("reason_codes") or [])
        return "FAILED", reasons[:160] or "RUNNER_HEALTH_FAILED"
    return "REVIEW", f"UNKNOWN_RUN_HEALTH:{health}"[:160]


async def run_j1_cycle(
    *,
    source: str,
    max_lateness_minutes: int = DEFAULT_MAX_LATENESS_MINUTES,
    max_fixtures: int = DEFAULT_MAX_FIXTURES,
) -> dict[str, Any]:
    """Run one J1 cycle with a database advisory lock and persistent heartbeat.

    The lock makes the Render cron and the GitHub Actions fallback safe to
    overlap. Only one process is allowed to execute the mutable J1 pipeline at a
    time. A locked-out invocation records a heartbeat but performs no writes.
    Runner health is persisted so an HTTP 200 cannot hide an internal J1 failure.
    """

    cycle_started = perf_counter()
    capacity = activate_j1_runner_capacity()
    configured_max = int(capacity["configured_max_fixtures"])
    if max_fixtures > configured_max:
        raise ValueError(
            f"max_fixtures={max_fixtures} exceeds configured J1 operational cap {configured_max}"
        )

    install_j1_pending_selector_v2()
    install_prediction_window_policy()

    run_id = _start_run(source)
    connection = engine.connect()
    lock_acquired = False
    try:
        lock_acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": J1_ADVISORY_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            counts = {"lock_skipped": 1}
            _finish_run(
                run_id,
                status="SKIPPED_LOCKED",
                selected_fixtures=0,
                counts=counts,
            )
            return {
                "status": "ok",
                "version": J1_SCHEDULER_VERSION,
                "selected_fixtures": 0,
                "counts": counts,
                "run_health": {
                    "status": "IDLE",
                    "reason_codes": ["J1_RUNNER_ALREADY_ACTIVE"],
                },
                "scheduler": {
                    "source": source,
                    "run_id": run_id,
                    "lock_acquired": False,
                    "reason": "J1_RUNNER_ALREADY_ACTIVE",
                    "fixture_capacity": capacity,
                    "pending_selector_version": J1_PENDING_SELECTOR_VERSION,
                    "prediction_window_policy_version": PREDICTION_WINDOW_POLICY_VERSION,
                },
            }

        recovery_audit = quarantine_invalid_reserved_j1_predictions(
            now=datetime.now(timezone.utc),
            prediction_window=J1_PREDICTION_WINDOW,
            model_version=MODEL_VERSION,
            target_lead_minutes=J1_TARGET_LEAD_MINUTES,
            max_lateness_minutes=max_lateness_minutes,
        )

        with authorized_prediction_producer(DAILY_PREDICTION_RUNNER_VERSION):
            result = await run_daily_prediction_runner(
                max_lateness_minutes=max_lateness_minutes,
                max_fixtures=max_fixtures,
            )

        result["prediction_window_policy"] = recovery_audit
        counts = dict(result.get("counts") or {})
        counts["invalid_reserved_predictions_quarantined"] = int(
            recovery_audit.get("quarantined_count") or 0
        )
        result["counts"] = counts
        selected = int(result.get("selected_fixtures") or 0)
        scheduler_status, scheduler_error = _scheduler_status_from_result(result)
        counts["run_health_status"] = (result.get("run_health") or {}).get("status")
        _finish_run(
            run_id,
            status=scheduler_status,
            selected_fixtures=selected,
            counts=counts,
            error=scheduler_error,
        )
        performance = result.setdefault("performance", {})
        performance["observatory"] = record_j1_result(
            result,
            source=source,
            run_id=run_id,
            scheduler_status=scheduler_status,
        )
        result["scheduler"] = {
            "version": J1_SCHEDULER_VERSION,
            "source": source,
            "run_id": run_id,
            "lock_acquired": True,
            "status": scheduler_status,
            "fixture_capacity": capacity,
            "pending_selector_version": J1_PENDING_SELECTOR_VERSION,
            "prediction_window_policy_version": PREDICTION_WINDOW_POLICY_VERSION,
        }
        return result
    except Exception as exc:
        _finish_run(
            run_id,
            status="FAILED",
            error=exc.__class__.__name__,
        )
        try_persist_pipeline_sample(
            pipeline=PIPELINE_J1,
            source=source,
            status="FAILED",
            cycle_seconds=perf_counter() - cycle_started,
            run_id=run_id,
            raw_metrics={"error": exc.__class__.__name__},
        )
        raise
    finally:
        if lock_acquired:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": J1_ADVISORY_LOCK_KEY},
                )
            except Exception:
                pass
        connection.close()


async def run_primary_operations_cycle() -> dict[str, Any]:
    result = await run_j1_cycle(
        source="render_cron",
        max_lateness_minutes=DEFAULT_MAX_LATENESS_MINUTES,
        max_fixtures=configured_j1_max_fixtures(),
    )

    # Render's existing cron still invokes `python -m app.j1_scheduler` even
    # though render.yaml points at j1_scheduler_v2. Keep Closing/CLV active on
    # this legacy command until the Render service definition is reconciled.
    try:
        from app.odds_window_clv import run_odds_window_clv_cycle

        result["odds_window_clv"] = await run_odds_window_clv_cycle()
    except Exception as exc:
        # Closing evidence is valuable but must never invalidate a successful
        # J1 Prediction/Decision/Ledger cycle.
        result["odds_window_clv"] = {
            "status": "failed",
            "version": "odds_window_clv_v1",
            "error": exc.__class__.__name__,
        }
    return result


def main() -> None:
    result = asyncio.run(run_primary_operations_cycle())
    print(json.dumps(result, ensure_ascii=False, default=str))
    health = str((result.get("run_health") or {}).get("status") or "UNKNOWN")
    if result.get("status") != "ok" or health == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()