from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app import daily_prediction_runner as legacy
from app.database import Base, SessionLocal, engine
from app.j1_pending_selector_v2 import select_pending_j1_fixtures

J1_WORK_QUEUE_VERSION = "j1_work_queue_v1"

STATUS_PENDING = "PENDING"
STATUS_CLAIMED = "CLAIMED"
STATUS_RETRY = "RETRY"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_EXPIRED = "EXPIRED"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_EXPIRED}
DEFAULT_CLAIM_LEASE_SECONDS = 180
DEFAULT_RETRY_DELAY_SECONDS = 15
DEFAULT_MAX_ATTEMPTS = 5

_schema_lock = Lock()
_schema_ready = False


class J1WorkItem(Base):
    __tablename__ = "j1_work_items"
    __table_args__ = (
        UniqueConstraint(
            "fixture_id",
            "snapshot_window",
            name="uq_j1_work_fixture_window",
        ),
    )

    # Production stays BIGINT. SQLite needs INTEGER PRIMARY KEY semantics in
    # unit tests so inserts can exercise the real claim transitions.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(),
        primary_key=True,
    )
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    sportmonks_fixture_id: Mapped[int] = mapped_column(BigInteger, index=True)
    snapshot_window: Mapped[str] = mapped_column(String(30), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    claimed_by: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_status: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_j1_work_queue_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        J1WorkItem.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def work_item_dict(
    row: J1WorkItem,
    *,
    include_claim_token: bool = False,
) -> dict[str, Any]:
    payload = {
        "id": int(row.id),
        "fixture_id": int(row.fixture_id),
        "sportmonks_fixture_id": int(row.sportmonks_fixture_id),
        "snapshot_window": row.snapshot_window,
        "due_at": _aware_utc(row.due_at).isoformat(),
        "kickoff_at": _aware_utc(row.kickoff_at).isoformat(),
        "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "available_at": _aware_utc(row.available_at).isoformat(),
        "claimed_by": row.claimed_by,
        "claimed_at": _aware_utc(row.claimed_at).isoformat() if row.claimed_at else None,
        "lease_expires_at": (
            _aware_utc(row.lease_expires_at).isoformat() if row.lease_expires_at else None
        ),
        "finished_at": _aware_utc(row.finished_at).isoformat() if row.finished_at else None,
        "result_status": row.result_status,
        "last_error": row.last_error,
    }
    if include_claim_token:
        payload["claim_token"] = row.claim_token
    return payload


def enqueue_due_j1_work(
    *,
    now: datetime | None = None,
    max_lateness_minutes: int,
    max_fixtures: int,
) -> dict[str, Any]:
    """Materialize due, unrecorded J1 fixtures as idempotent queue rows."""

    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    fixtures, selector_audit = select_pending_j1_fixtures(
        now=effective_now,
        max_lateness_minutes=max_lateness_minutes,
        max_fixtures=max_fixtures,
    )

    created = 0
    existing = 0
    items: list[dict[str, Any]] = []
    with SessionLocal() as session:
        for fixture in fixtures:
            snapshot_window = legacy._snapshot_window(fixture)
            row = session.scalar(
                select(J1WorkItem)
                .where(
                    J1WorkItem.fixture_id == int(fixture.id),
                    J1WorkItem.snapshot_window == snapshot_window,
                )
                .limit(1)
            )
            if row is None:
                kickoff_at = _aware_utc(fixture.starts_at)
                row = J1WorkItem(
                    fixture_id=int(fixture.id),
                    sportmonks_fixture_id=int(fixture.sportmonks_id),
                    snapshot_window=snapshot_window,
                    due_at=kickoff_at - timedelta(minutes=legacy.J1_TARGET_LEAD_MINUTES),
                    kickoff_at=kickoff_at,
                    status=STATUS_PENDING,
                    attempt_count=0,
                    available_at=effective_now,
                    result_payload={},
                )
                session.add(row)
                session.flush()
                created += 1
            else:
                existing += 1
            # Claim tokens are capabilities and never leave the worker path.
            items.append(work_item_dict(row))
        session.commit()

    return {
        "status": "ok",
        "version": J1_WORK_QUEUE_VERSION,
        "evaluated_at": effective_now.isoformat(),
        "selected_fixtures": len(fixtures),
        "enqueued": created,
        "already_queued": existing,
        "selector": selector_audit,
        "items": items,
        "policy": {
            "queue_backend": "postgres",
            "claim_strategy": "for_update_skip_locked",
            "unique_fixture_snapshot_window": True,
            "claim_tokens_not_exposed_in_producer_payloads": True,
            "ledger_remains_final_idempotency_authority": True,
        },
    }


def expire_past_kickoff_work(*, now: datetime | None = None) -> int:
    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    expired = 0
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(J1WorkItem).where(
                    J1WorkItem.status.in_([STATUS_PENDING, STATUS_RETRY, STATUS_CLAIMED]),
                    J1WorkItem.kickoff_at <= effective_now,
                )
            ).all()
        )
        for row in rows:
            row.status = STATUS_EXPIRED
            row.finished_at = effective_now
            row.result_status = row.result_status or "KICKOFF_REACHED_BEFORE_COMPLETION"
            row.claimed_by = None
            row.claim_token = None
            row.claimed_at = None
            row.lease_expires_at = None
            expired += 1
        session.commit()
    return expired


def claim_next_j1_work(
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Atomically claim one fixture. PostgreSQL uses SKIP LOCKED for scale-out."""

    if not worker_id.strip():
        raise ValueError("worker_id must not be empty")
    if lease_seconds < 30 or lease_seconds > 1800:
        raise ValueError("lease_seconds must be between 30 and 1800")

    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    lease_until = effective_now + timedelta(seconds=lease_seconds)

    with SessionLocal() as session:
        claimable = or_(
            and_(
                J1WorkItem.status.in_([STATUS_PENDING, STATUS_RETRY]),
                J1WorkItem.available_at <= effective_now,
            ),
            and_(
                J1WorkItem.status == STATUS_CLAIMED,
                J1WorkItem.lease_expires_at.is_not(None),
                J1WorkItem.lease_expires_at <= effective_now,
            ),
        )
        row = session.scalar(
            select(J1WorkItem)
            .where(claimable, J1WorkItem.kickoff_at > effective_now)
            .order_by(J1WorkItem.due_at.asc(), J1WorkItem.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None

        row.status = STATUS_CLAIMED
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.claimed_by = worker_id[:160]
        row.claim_token = str(uuid.uuid4())
        row.claimed_at = effective_now
        row.lease_expires_at = lease_until
        row.last_error = None
        session.commit()
        session.refresh(row)
        return work_item_dict(row, include_claim_token=True)


def renew_j1_claim(
    *,
    work_id: int,
    claim_token: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
) -> bool:
    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    with SessionLocal() as session:
        row = session.scalar(
            select(J1WorkItem)
            .where(
                J1WorkItem.id == int(work_id),
                J1WorkItem.status == STATUS_CLAIMED,
                J1WorkItem.claim_token == claim_token,
            )
            .with_for_update()
            .limit(1)
        )
        if row is None:
            return False
        row.lease_expires_at = effective_now + timedelta(seconds=lease_seconds)
        session.commit()
        return True


def complete_j1_work(
    *,
    work_id: int,
    claim_token: str,
    result_status: str,
    result_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    with SessionLocal() as session:
        row = session.scalar(
            select(J1WorkItem)
            .where(
                J1WorkItem.id == int(work_id),
                J1WorkItem.status == STATUS_CLAIMED,
                J1WorkItem.claim_token == claim_token,
            )
            .with_for_update()
            .limit(1)
        )
        if row is None:
            return False
        row.status = STATUS_COMPLETED
        row.finished_at = effective_now
        row.result_status = str(result_status)[:60]
        row.result_payload = dict(result_payload or {})
        row.claimed_by = None
        row.claim_token = None
        row.claimed_at = None
        row.lease_expires_at = None
        session.commit()
        return True


def fail_j1_work(
    *,
    work_id: int,
    claim_token: str,
    error: str,
    result_status: str,
    retryable: bool,
    result_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any] | None:
    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    with SessionLocal() as session:
        row = session.scalar(
            select(J1WorkItem)
            .where(
                J1WorkItem.id == int(work_id),
                J1WorkItem.status == STATUS_CLAIMED,
                J1WorkItem.claim_token == claim_token,
            )
            .with_for_update()
            .limit(1)
        )
        if row is None:
            return None

        retry_at = effective_now + timedelta(seconds=max(1, retry_delay_seconds))
        can_retry = (
            retryable
            and int(row.attempt_count or 0) < max_attempts
            and retry_at < _aware_utc(row.kickoff_at)
        )
        row.result_status = str(result_status)[:60]
        row.last_error = str(error)[:200]
        row.result_payload = dict(result_payload or {})
        row.claimed_by = None
        row.claim_token = None
        row.claimed_at = None
        row.lease_expires_at = None
        if can_retry:
            row.status = STATUS_RETRY
            row.available_at = retry_at
        else:
            row.status = STATUS_FAILED
            row.finished_at = effective_now
        session.commit()
        session.refresh(row)
        return work_item_dict(row)


def queue_status(*, now: datetime | None = None) -> dict[str, Any]:
    ensure_j1_work_queue_schema()
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    statuses = [
        STATUS_PENDING,
        STATUS_CLAIMED,
        STATUS_RETRY,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_EXPIRED,
    ]
    counts = {status: 0 for status in statuses}
    with SessionLocal() as session:
        rows = session.execute(
            select(J1WorkItem.status, func.count(J1WorkItem.id)).group_by(J1WorkItem.status)
        ).all()
        for status, count in rows:
            counts[str(status)] = int(count)
    return {
        "status": "ok",
        "version": J1_WORK_QUEUE_VERSION,
        "evaluated_at": effective_now.isoformat(),
        "counts": counts,
    }
