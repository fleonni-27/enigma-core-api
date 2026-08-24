from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine
from app.daily_prediction_runner import (
    DEFAULT_MAX_FIXTURES,
    DEFAULT_MAX_LATENESS_MINUTES,
    run_daily_prediction_runner,
)

J1_SCHEDULER_VERSION = "j1_scheduler_v1"
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


async def run_j1_cycle(
    *,
    source: str,
    max_lateness_minutes: int = DEFAULT_MAX_LATENESS_MINUTES,
    max_fixtures: int = DEFAULT_MAX_FIXTURES,
) -> dict[str, Any]:
    """Run one J1 cycle with a database advisory lock and persistent heartbeat.

    The lock makes the Render cron and the legacy GitHub Actions fallback safe to
    overlap. Only one process is allowed to execute the mutable J1 pipeline at a
    time. A locked-out invocation records a heartbeat but performs no writes.
    """

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
                "scheduler": {
                    "source": source,
                    "run_id": run_id,
                    "lock_acquired": False,
                    "reason": "J1_RUNNER_ALREADY_ACTIVE",
                },
            }

        result = await run_daily_prediction_runner(
            max_lateness_minutes=max_lateness_minutes,
            max_fixtures=max_fixtures,
        )
        counts = dict(result.get("counts") or {})
        selected = int(result.get("selected_fixtures") or 0)
        _finish_run(
            run_id,
            status="OK",
            selected_fixtures=selected,
            counts=counts,
        )
        result["scheduler"] = {
            "version": J1_SCHEDULER_VERSION,
            "source": source,
            "run_id": run_id,
            "lock_acquired": True,
        }
        return result
    except Exception as exc:
        _finish_run(
            run_id,
            status="FAILED",
            error=exc.__class__.__name__,
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


def main() -> None:
    result = asyncio.run(
        run_j1_cycle(
            source="render_cron",
            max_lateness_minutes=DEFAULT_MAX_LATENESS_MINUTES,
            max_fixtures=DEFAULT_MAX_FIXTURES,
        )
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
