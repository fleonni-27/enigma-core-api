from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, DateTime, Identity, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.daily_operations import BUSINESS_TIMEZONE, run_daily_sync
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
J1_EXECUTION_MODE_ENV = "J1_EXECUTION_MODE"
J1_EXECUTION_MODE_BATCH = "batch"
J1_EXECUTION_MODE_PRODUCER = "producer"
VALID_J1_EXECUTION_MODES = {J1_EXECUTION_MODE_BATCH, J1_EXECUTION_MODE_PRODUCER}
FIXTURE_COVERAGE_OPERATION_NAME = "DAILY_FIXTURE_COVERAGE_SYNC"
FIXTURE_COVERAGE_ADVISORY_LOCK_KEY = 450027
FIXTURE_COVERAGE_INTERVAL_MINUTES = 15

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


def _start_run(source: str, *, operation: str = J1_OPERATION_NAME) -> int:
    ensure_operation_run_schema()
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        row = OperationRunRecord(
            operation=operation,
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


def configured_j1_execution_mode() -> str:
    mode = str(os.getenv(J1_EXECUTION_MODE_ENV, J1_EXECUTION_MODE_BATCH)).strip().lower()
    if mode not in VALID_J1_EXECUTION_MODES:
        raise ValueError(
            f"{J1_EXECUTION_MODE_ENV} must be one of {sorted(VALID_J1_EXECUTION_MODES)}"
        )
    return mode


def _latest_fixture_coverage_sync() -> OperationRunRecord | None:
    ensure_operation_run_schema()
    with SessionLocal() as session:
        row = session.scalar(
            select(OperationRunRecord)
            .where(
                OperationRunRecord.operation == FIXTURE_COVERAGE_OPERATION_NAME,
                OperationRunRecord.status.in_(["OK", "DEGRADED"]),
            )
            .order_by(OperationRunRecord.started_at.desc(), OperationRunRecord.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        session.expunge(row)
        return row


async def maybe_run_fixture_coverage_sync() -> dict[str, Any]:
    """Refresh today's fixture list without coupling odds/predictions to this cadence.

    Render already wakes the J1 scheduler every minute. This lightweight guard uses
    that reliable process as a fallback fixture-discovery source at most every 15
    minutes. It fetches fixtures only (no broad odds refresh), so the official J1
    odds/prediction/decision timing remains unchanged.
    """

    now = datetime.now(timezone.utc)
    latest = _latest_fixture_coverage_sync()
    if latest is not None and latest.started_at >= now - timedelta(minutes=FIXTURE_COVERAGE_INTERVAL_MINUTES):
        return {
            "status": "ok",
            "action": "skipped_recent",
            "interval_minutes": FIXTURE_COVERAGE_INTERVAL_MINUTES,
            "last_started_at": latest.started_at.isoformat(),
        }

    connection = engine.connect()
    lock_acquired = False
    run_id: int | None = None
    try:
        lock_acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": FIXTURE_COVERAGE_ADVISORY_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            return {
                "status": "ok",
                "action": "skipped_locked",
                "interval_minutes": FIXTURE_COVERAGE_INTERVAL_MINUTES,
            }

        latest = _latest_fixture_coverage_sync()
        now = datetime.now(timezone.utc)
        if latest is not None and latest.started_at >= now - timedelta(minutes=FIXTURE_COVERAGE_INTERVAL_MINUTES):
            return {
                "status": "ok",
                "action": "skipped_recent_after_lock",
                "interval_minutes": FIXTURE_COVERAGE_INTERVAL_MINUTES,
                "last_started_at": latest.started_at.isoformat(),
            }

        run_id = _start_run("render_cron", operation=FIXTURE_COVERAGE_OPERATION_NAME)
        target_date = datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()
        sync = await run_daily_sync(target_date=target_date, refresh_odds=False)
        target_fixtures = int((sync.get("target_fixtures") or {}).get("count") or 0)
        fixture_ingestion = sync.get("fixture_ingestion") or {}
        sync_status = str(sync.get("status") or "unknown")
        persisted_status = "OK" if sync_status == "ok" else "DEGRADED"
        counts = {
            "target_fixtures": target_fixtures,
            "received": int(fixture_ingestion.get("received") or 0),
            "created": int(fixture_ingestion.get("created") or 0),
            "updated": int(fixture_ingestion.get("updated") or 0),
            "skipped": int(fixture_ingestion.get("skipped") or 0),
        }
        _finish_run(
            run_id,
            status=persisted_status,
            selected_fixtures=target_fixtures,
            counts=counts,
        )
        return {
            "status": sync_status,
            "action": "refreshed",
            "target_date": target_date.isoformat(),
            "target_fixtures": target_fixtures,
            "fixture_ingestion": fixture_ingestion,
            "interval_minutes": FIXTURE_COVERAGE_INTERVAL_MINUTES,
            "run_id": run_id,
            "odds_refresh_requested": False,
        }
    except Exception as exc:
        if run_id is not None:
            _finish_run(run_id, status="FAILED", error=exc.__class__.__name__)
        return {
            "status": "failed",
            "action": "coverage_sync_failed",
            "error": exc.__class__.__name__,
            "interval_minutes": FIXTURE_COVERAGE_INTERVAL_MINUTES,
        }
    finally:
        if lock_acquired:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": FIXTURE_COVERAGE_ADVISORY_LOCK_KEY},
                )
            except Exception:
                pass
        connection.close()


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

    try:
        from app.odds_window_clv import run_odds_window_clv_cycle

        result["odds_window_clv"] = await run_odds_window_clv_cycle()
    except Exception as exc:
        result["odds_window_clv"] = {
            "status": "failed",
            "version": "odds_window_clv_v1",
            "error": exc.__class__.__name__,
        }
    return result


async def run_render_cron_entrypoint() -> dict[str, Any]:
    """Execute fixture discovery, then batch or producer mode.

    Fixture discovery deliberately happens before the execution-mode branch so it
    remains active when production is in producer mode. This only refreshes today's
    fixture catalog; official odds/prediction/decision timing stays in J1.
    """

    fixture_coverage_sync = await maybe_run_fixture_coverage_sync()
    mode = configured_j1_execution_mode()
    if mode == J1_EXECUTION_MODE_PRODUCER:
        from app.j1_work_producer import run_producer_cycle

        result = await run_producer_cycle()
    else:
        result = await run_primary_operations_cycle()

    result["fixture_coverage_sync"] = fixture_coverage_sync
    execution = result.setdefault("execution", {})
    execution.update(
        {
            "mode": mode,
            "environment_variable": J1_EXECUTION_MODE_ENV,
            "render_start_command_compatible": "python -m app.j1_scheduler",
            "canonical_producer_module": "app.j1_work_producer",
        }
    )
    return result


def main() -> None:
    result = asyncio.run(run_render_cron_entrypoint())
    print(json.dumps(result, ensure_ascii=False, default=str))
    health = str((result.get("run_health") or {}).get("status") or "UNKNOWN")
    if result.get("status") != "ok" or health == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
