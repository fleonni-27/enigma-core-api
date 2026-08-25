from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, UniqueConstraint, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine
from app.enigma_rating_v2_frozen_params import (
    CONFIRMATION_HOLDOUT_START_DATE,
    CONFIRMATION_MIN_ELIGIBLE_TARGETS,
    FROZEN_SELECTION_SHA256,
    FROZEN_TUNING_VERSION,
    SELECTION_LEAGUES,
)
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.league_registry import canonical_league
from app.models import Fixture, Prediction

CONFIRMATION_HOLDOUT_VERSION = "enigma_rating_v2_confirmation_holdout_v1"
HOLDOUT_TIMEZONE = "America/Sao_Paulo"
HOLDOUT_J1_PREDICTION_WINDOW = "j1_45m_v1"
HOLDOUT_LEDGER_SOURCE = "daily_prediction_runner_v1"
HOLDOUT_J1_LEAD_MINUTES = 45

router = APIRouter(prefix="/research/enigma-rating-v2", tags=["research"])
logger = logging.getLogger(__name__)

_schema_lock = Lock()
_schema_ready = False


class ConfirmationHoldoutTarget(Base):
    __tablename__ = "confirmation_holdout_targets"
    __table_args__ = (
        UniqueConstraint(
            "selection_sha256",
            "fixture_id",
            name="uq_confirmation_holdout_selection_fixture",
        ),
        UniqueConstraint(
            "selection_sha256",
            "target_number",
            name="uq_confirmation_holdout_selection_number",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    selection_sha256: Mapped[str] = mapped_column(String(64), index=True)
    holdout_version: Mapped[str] = mapped_column(String(80), index=True)
    frozen_tuning_version: Mapped[str] = mapped_column(String(80))

    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    sportmonks_fixture_id: Mapped[int] = mapped_column(BigInteger, index=True)
    league: Mapped[str] = mapped_column(String(160), index=True)
    home_team: Mapped[str] = mapped_column(String(160))
    away_team: Mapped[str] = mapped_column(String(160))
    fixture_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    target_number: Mapped[int] = mapped_column(Integer, index=True)
    prediction_id: Mapped[int] = mapped_column(BigInteger, index=True)
    prediction_window: Mapped[str] = mapped_column(String(30), index=True)
    model_version: Mapped[str] = mapped_column(String(80), index=True)
    snapshot_window: Mapped[str] = mapped_column(String(30), index=True)
    ledger_record_id: Mapped[int] = mapped_column(BigInteger, index=True)
    capture_evidence: Mapped[dict] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


def ensure_confirmation_holdout_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        ConfirmationHoldoutTarget.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _holdout_start_utc() -> datetime:
    local_date = date.fromisoformat(CONFIRMATION_HOLDOUT_START_DATE)
    local_start = datetime.combine(local_date, time.min, tzinfo=ZoneInfo(HOLDOUT_TIMEZONE))
    return local_start.astimezone(timezone.utc)


def _frozen_league_keys() -> set[str]:
    keys: set[str] = set()
    for league in SELECTION_LEAGUES:
        canonical = canonical_league(league)
        key = canonical.get("key")
        if key:
            keys.add(str(key))
    return keys


def _candidate_metadata(*, starts_at: datetime, league_name: str | None) -> dict[str, Any]:
    starts_at_utc = _aware_utc(starts_at)
    canonical = canonical_league(str(league_name or ""))
    key = str(canonical.get("key") or "")
    reasons: list[str] = []
    if starts_at_utc < _holdout_start_utc():
        reasons.append("BEFORE_CONFIRMATION_HOLDOUT_START")
    if not key or key not in _frozen_league_keys():
        reasons.append("LEAGUE_NOT_IN_FROZEN_SELECTION")
    return {
        "candidate": not reasons,
        "reason_codes": reasons,
        "canonical_league": canonical.get("canonical_name") or league_name,
        "league_key": key or None,
    }


def _acquire_numbering_lock(session) -> None:
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"confirmation_holdout:{FROZEN_SELECTION_SHA256}"},
        )


def _target_payload(row: ConfirmationHoldoutTarget, *, settlement_status: str | None) -> dict[str, Any]:
    settled = str(settlement_status or "").upper() == "SETTLED"
    return {
        "target_number": int(row.target_number),
        "fixture_id": int(row.fixture_id),
        "sportmonks_fixture_id": int(row.sportmonks_fixture_id),
        "league": row.league,
        "home_team": row.home_team,
        "away_team": row.away_team,
        "starts_at": row.fixture_starts_at.isoformat() if row.fixture_starts_at else None,
        "state": "SETTLED_TARGET" if settled else "CAPTURED_PREMATCH",
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "selection_sha256": row.selection_sha256,
        "holdout_version": row.holdout_version,
    }


def capture_confirmation_holdout_target(ledger_record_id: int) -> dict[str, Any]:
    """Idempotently register one immutable J1 forward record in the frozen holdout."""

    ensure_forward_test_schema()
    ensure_confirmation_holdout_schema()

    with SessionLocal() as session:
        joined = session.execute(
            select(DecisionRecord, Fixture, Prediction)
            .join(Fixture, Fixture.id == DecisionRecord.fixture_id)
            .join(Prediction, Prediction.id == DecisionRecord.prediction_id)
            .where(DecisionRecord.id == int(ledger_record_id))
        ).first()
        if joined is None:
            return {
                "status": "not_captured",
                "version": CONFIRMATION_HOLDOUT_VERSION,
                "reason_codes": ["LEDGER_RECORD_NOT_FOUND"],
            }

        record, fixture, prediction = joined
        candidate = _candidate_metadata(
            starts_at=fixture.starts_at,
            league_name=fixture.league_name,
        )
        reasons = list(candidate["reason_codes"])
        starts_at = _aware_utc(fixture.starts_at)
        j1_due_at = starts_at - timedelta(minutes=HOLDOUT_J1_LEAD_MINUTES)
        generated_at = _aware_utc(prediction.generated_at)
        recorded_at = _aware_utc(record.recorded_at)

        if str(record.prediction_window) != HOLDOUT_J1_PREDICTION_WINDOW:
            reasons.append("NOT_RESERVED_J1_PREDICTION_WINDOW")
        if str(record.source) != HOLDOUT_LEDGER_SOURCE:
            reasons.append("NOT_IMMUTABLE_J1_LEDGER_SOURCE")
        if generated_at < j1_due_at:
            reasons.append("PREDICTION_GENERATED_BEFORE_J1_DUE")
        if generated_at >= starts_at:
            reasons.append("PREDICTION_NOT_PREMATCH")
        if recorded_at >= starts_at:
            reasons.append("LEDGER_NOT_RECORDED_PREMATCH")

        if reasons:
            return {
                "status": "not_candidate",
                "version": CONFIRMATION_HOLDOUT_VERSION,
                "reason_codes": sorted(set(reasons)),
                "fixture_id": int(fixture.id),
            }

        _acquire_numbering_lock(session)
        existing = session.scalar(
            select(ConfirmationHoldoutTarget).where(
                ConfirmationHoldoutTarget.selection_sha256 == FROZEN_SELECTION_SHA256,
                ConfirmationHoldoutTarget.fixture_id == int(fixture.id),
            )
        )
        if existing is not None:
            settlement_status = session.scalar(
                select(DecisionRecord.settlement_status).where(
                    DecisionRecord.id == int(existing.ledger_record_id)
                )
            )
            return {
                "status": "exists",
                "version": CONFIRMATION_HOLDOUT_VERSION,
                "target": _target_payload(existing, settlement_status=settlement_status),
            }

        current_max = session.scalar(
            select(func.max(ConfirmationHoldoutTarget.target_number)).where(
                ConfirmationHoldoutTarget.selection_sha256 == FROZEN_SELECTION_SHA256
            )
        )
        target_number = int(current_max or 0) + 1
        canonical_league = str(candidate["canonical_league"] or fixture.league_name or "Unknown")
        row = ConfirmationHoldoutTarget(
            selection_sha256=FROZEN_SELECTION_SHA256,
            holdout_version=CONFIRMATION_HOLDOUT_VERSION,
            frozen_tuning_version=FROZEN_TUNING_VERSION,
            fixture_id=int(fixture.id),
            sportmonks_fixture_id=int(fixture.sportmonks_id),
            league=canonical_league,
            home_team=str(fixture.home_team),
            away_team=str(fixture.away_team),
            fixture_starts_at=starts_at,
            target_number=target_number,
            prediction_id=int(prediction.id),
            prediction_window=str(record.prediction_window),
            model_version=str(record.model_version),
            snapshot_window=str(record.snapshot_window),
            ledger_record_id=int(record.id),
            capture_evidence={
                "selection_sha256": FROZEN_SELECTION_SHA256,
                "prediction_generated_at": generated_at.isoformat(),
                "decision_recorded_at": recorded_at.isoformat(),
                "j1_due_at": j1_due_at.isoformat(),
                "kickoff_at": starts_at.isoformat(),
                "prediction_generated_at_or_after_j1_due": generated_at >= j1_due_at,
                "prediction_generated_before_kickoff": generated_at < starts_at,
                "ledger_recorded_before_kickoff": recorded_at < starts_at,
                "inclusion_does_not_depend_on_match_result": True,
                "performance_metrics_captured": False,
            },
        )
        session.add(row)
        try:
            session.commit()
            session.refresh(row)
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(ConfirmationHoldoutTarget).where(
                    ConfirmationHoldoutTarget.selection_sha256 == FROZEN_SELECTION_SHA256,
                    ConfirmationHoldoutTarget.fixture_id == int(fixture.id),
                )
            )
            if existing is None:
                raise
            settlement_status = session.scalar(
                select(DecisionRecord.settlement_status).where(
                    DecisionRecord.id == int(existing.ledger_record_id)
                )
            )
            return {
                "status": "exists",
                "version": CONFIRMATION_HOLDOUT_VERSION,
                "target": _target_payload(existing, settlement_status=settlement_status),
            }

        return {
            "status": "captured",
            "version": CONFIRMATION_HOLDOUT_VERSION,
            "target": _target_payload(row, settlement_status=record.settlement_status),
            "policy": {
                "captured_prematch": True,
                "inclusion_result_blind": True,
                "performance_peeking": False,
            },
        }


def capture_confirmation_holdout_for_fixture_window(
    *, fixture_id: int, snapshot_window: str
) -> dict[str, Any]:
    ensure_forward_test_schema()
    with SessionLocal() as session:
        record_id = session.scalar(
            select(DecisionRecord.id)
            .where(
                DecisionRecord.fixture_id == int(fixture_id),
                DecisionRecord.snapshot_window == str(snapshot_window),
                DecisionRecord.source == HOLDOUT_LEDGER_SOURCE,
                DecisionRecord.prediction_window == HOLDOUT_J1_PREDICTION_WINDOW,
            )
            .order_by(DecisionRecord.recorded_at.asc(), DecisionRecord.id.asc())
            .limit(1)
        )
    if record_id is None:
        return {
            "status": "not_captured",
            "version": CONFIRMATION_HOLDOUT_VERSION,
            "reason_codes": ["J1_LEDGER_RECORD_NOT_FOUND"],
        }
    return capture_confirmation_holdout_target(int(record_id))


def reconcile_confirmation_holdout_targets(*, limit: int = 5000) -> dict[str, Any]:
    """Recover only provably pre-match immutable J1 records; results never drive inclusion."""

    ensure_forward_test_schema()
    ensure_confirmation_holdout_schema()
    with SessionLocal() as session:
        record_ids = list(
            session.scalars(
                select(DecisionRecord.id)
                .where(
                    DecisionRecord.fixture_starts_at >= _holdout_start_utc(),
                    DecisionRecord.source == HOLDOUT_LEDGER_SOURCE,
                    DecisionRecord.prediction_window == HOLDOUT_J1_PREDICTION_WINDOW,
                )
                .order_by(DecisionRecord.fixture_starts_at.asc(), DecisionRecord.id.asc())
                .limit(int(limit))
            ).all()
        )

    counts: dict[str, int] = {}
    for record_id in record_ids:
        result = capture_confirmation_holdout_target(int(record_id))
        key = str(result.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "status": "ok",
        "version": CONFIRMATION_HOLDOUT_VERSION,
        "records_scanned": len(record_ids),
        "counts": counts,
        "policy": {
            "reconciliation_uses_only_immutable_prematch_evidence": True,
            "settlement_or_result_never_controls_inclusion": True,
        },
    }


def confirmation_holdout_status(*, include_targets: bool = True) -> dict[str, Any]:
    ensure_forward_test_schema()
    ensure_confirmation_holdout_schema()
    with SessionLocal() as session:
        rows = session.execute(
            select(ConfirmationHoldoutTarget, DecisionRecord.settlement_status, DecisionRecord.actual_result)
            .outerjoin(
                DecisionRecord,
                DecisionRecord.id == ConfirmationHoldoutTarget.ledger_record_id,
            )
            .where(ConfirmationHoldoutTarget.selection_sha256 == FROZEN_SELECTION_SHA256)
            .order_by(ConfirmationHoldoutTarget.target_number.asc())
        ).all()

    captured = len(rows)
    settled_rows = [
        row
        for row in rows
        if str(row.settlement_status or "").upper() == "SETTLED"
        and str(row.actual_result or "") in {"1", "X", "2"}
    ]
    settled = len(settled_rows)
    minimum = int(CONFIRMATION_MIN_ELIGIBLE_TARGETS)
    ready = settled >= minimum
    summary: dict[str, Any] = {
        "status": "READY_FOR_CONFIRMATION" if ready else "ACCUMULATING_NO_PEEKING",
        "version": CONFIRMATION_HOLDOUT_VERSION,
        "frozen_tuning_version": FROZEN_TUNING_VERSION,
        "selection_sha256": FROZEN_SELECTION_SHA256,
        "start_date": CONFIRMATION_HOLDOUT_START_DATE,
        "timezone": HOLDOUT_TIMEZONE,
        "minimum_eligible_targets": minimum,
        "captured_targets": captured,
        "settled_eligible_targets": settled,
        "remaining_to_confirmation": max(0, minimum - settled),
        "progress_pct": round(min(1.0, settled / minimum) * 100.0, 1),
        "progress_counter": f"{min(settled, minimum)}/{minimum}",
        "ready_for_confirmation": ready,
        "performance_metrics_visible": False,
        "metrics_locked_until_minimum_targets": True,
        "policy": {
            "no_peeking": True,
            "inclusion_is_prematch_and_result_blind": True,
            "settlement_only_advances_progress_not_selection": True,
            "retuning_with_holdout_data_allowed": False,
            "dashboard_exposes_no_brier_logloss_accuracy_or_calibration": True,
        },
    }
    if include_targets:
        summary["targets"] = [
            _target_payload(row[0], settlement_status=row.settlement_status)
            for row in rows
        ]
    return summary


def confirmation_holdout_dashboard_state(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    summary = confirmation_holdout_status(include_targets=False)
    fixture_ids = [int(item["fixture_id"]) for item in fixtures if item.get("fixture_id") is not None]
    registered: dict[int, tuple[ConfirmationHoldoutTarget, str | None]] = {}
    if fixture_ids:
        with SessionLocal() as session:
            rows = session.execute(
                select(ConfirmationHoldoutTarget, DecisionRecord.settlement_status)
                .outerjoin(
                    DecisionRecord,
                    DecisionRecord.id == ConfirmationHoldoutTarget.ledger_record_id,
                )
                .where(
                    ConfirmationHoldoutTarget.selection_sha256 == FROZEN_SELECTION_SHA256,
                    ConfirmationHoldoutTarget.fixture_id.in_(fixture_ids),
                )
            ).all()
        registered = {
            int(row[0].fixture_id): (row[0], row.settlement_status)
            for row in rows
        }

    by_fixture_id: dict[int, dict[str, Any]] = {}
    for item in fixtures:
        fixture_id = int(item["fixture_id"])
        starts_at = datetime.fromisoformat(str(item["starts_at"]).replace("Z", "+00:00"))
        candidate = _candidate_metadata(starts_at=starts_at, league_name=item.get("league"))
        existing = registered.get(fixture_id)
        if existing is not None:
            target, settlement_status = existing
            payload = _target_payload(target, settlement_status=settlement_status)
            by_fixture_id[fixture_id] = {
                "candidate": True,
                "registered": True,
                "confirmed_eligible": True,
                "target_number": int(target.target_number),
                "status": payload["state"],
                "selection_sha256": FROZEN_SELECTION_SHA256,
                "no_peeking": True,
            }
        elif candidate["candidate"]:
            by_fixture_id[fixture_id] = {
                "candidate": True,
                "registered": False,
                "confirmed_eligible": False,
                "target_number": None,
                "status": "CANDIDATE_WAITING_J1",
                "selection_sha256": FROZEN_SELECTION_SHA256,
                "no_peeking": True,
            }
        else:
            by_fixture_id[fixture_id] = {
                "candidate": False,
                "registered": False,
                "confirmed_eligible": False,
                "target_number": None,
                "status": "NOT_IN_CONFIRMATION_HOLDOUT",
                "reason_codes": candidate["reason_codes"],
                "selection_sha256": FROZEN_SELECTION_SHA256,
                "no_peeking": True,
            }

    return {"summary": summary, "by_fixture_id": by_fixture_id}


def install_confirmation_holdout_startup(app) -> None:
    @app.on_event("startup")
    async def _reconcile_confirmation_holdout_on_startup() -> None:
        try:
            result = await asyncio.to_thread(reconcile_confirmation_holdout_targets)
            logger.info(
                "confirmation_holdout_reconciliation status=%s scanned=%s counts=%s",
                result.get("status"),
                result.get("records_scanned"),
                result.get("counts"),
            )
        except Exception:
            logger.exception("confirmation holdout startup reconciliation failed")


@router.get("/confirmation-holdout-v1/status")
def confirmation_holdout_status_endpoint() -> dict[str, Any]:
    return confirmation_holdout_status(include_targets=True)
