from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine

FIXTURE_RESULT_VERSION = "fixture_result_v1"

_schema_lock = Lock()
_schema_ready = False


class FixtureResultRecord(Base):
    __tablename__ = "fixture_result_records"
    __table_args__ = (
        UniqueConstraint(
            "sportmonks_fixture_id",
            name="uq_fixture_result_records_sportmonks_fixture_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    sportmonks_fixture_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    home_goals: Mapped[int] = mapped_column(Integer)
    away_goals: Mapped[int] = mapped_column(Integer)
    actual_result: Mapped[str] = mapped_column(String(2), index=True)
    score_source: Mapped[str] = mapped_column(String(30))
    state_id: Mapped[int | None] = mapped_column(Integer)
    state_code: Mapped[str | None] = mapped_column(String(30))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


def ensure_fixture_result_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        FixtureResultRecord.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def _payload(row: FixtureResultRecord) -> dict[str, Any]:
    return {
        "version": FIXTURE_RESULT_VERSION,
        "fixture_id": int(row.fixture_id),
        "sportmonks_fixture_id": int(row.sportmonks_fixture_id),
        "home_goals": int(row.home_goals),
        "away_goals": int(row.away_goals),
        "actual_result": row.actual_result,
        "score_source": row.score_source,
        "state_id": row.state_id,
        "state_code": row.state_code,
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
    }


def persist_fixture_result(
    *,
    fixture_id: int,
    sportmonks_fixture_id: int,
    home_goals: int,
    away_goals: int,
    actual_result: str,
    score_source: str,
    state_id: int | None,
    state_code: str | None,
) -> dict[str, Any]:
    ensure_fixture_result_schema()

    with SessionLocal() as session:
        existing = session.scalar(
            select(FixtureResultRecord).where(
                FixtureResultRecord.sportmonks_fixture_id == sportmonks_fixture_id
            )
        )
        if existing is not None:
            same = (
                int(existing.fixture_id) == int(fixture_id)
                and int(existing.home_goals) == int(home_goals)
                and int(existing.away_goals) == int(away_goals)
                and str(existing.actual_result) == str(actual_result)
            )
            return {
                "status": "exists" if same else "conflict",
                "record": _payload(existing),
                "incoming": {
                    "fixture_id": int(fixture_id),
                    "sportmonks_fixture_id": int(sportmonks_fixture_id),
                    "home_goals": int(home_goals),
                    "away_goals": int(away_goals),
                    "actual_result": str(actual_result),
                    "score_source": str(score_source),
                    "state_id": state_id,
                    "state_code": state_code,
                }
                if not same
                else None,
                "policy": {
                    "immutable_after_first_persist": True,
                    "conflicting_final_scores_are_never_overwritten": True,
                },
            }

        row = FixtureResultRecord(
            fixture_id=int(fixture_id),
            sportmonks_fixture_id=int(sportmonks_fixture_id),
            home_goals=int(home_goals),
            away_goals=int(away_goals),
            actual_result=str(actual_result),
            score_source=str(score_source),
            state_id=state_id,
            state_code=state_code,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "status": "persisted",
            "record": _payload(row),
            "policy": {
                "immutable_after_first_persist": True,
                "conflicting_final_scores_are_never_overwritten": True,
            },
        }


def fixture_results_by_sportmonks_ids(
    sportmonks_fixture_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not sportmonks_fixture_ids:
        return {}
    ensure_fixture_result_schema()
    ids = sorted({int(value) for value in sportmonks_fixture_ids})
    with SessionLocal() as session:
        rows = session.scalars(
            select(FixtureResultRecord).where(
                FixtureResultRecord.sportmonks_fixture_id.in_(ids)
            )
        ).all()
    return {int(row.sportmonks_fixture_id): _payload(row) for row in rows}
