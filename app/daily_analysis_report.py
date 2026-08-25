from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, Date, DateTime, Identity, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine
from app.forward_test_report_v3 import build_forward_test_report_v3
from app.outcome_settlement import settle_pending_records

DAILY_ANALYSIS_REPORT_VERSION = "daily_analysis_report_v1"
BUSINESS_TIMEZONE = "America/Sao_Paulo"
_schema_lock = Lock()
_schema_ready = False


class DailyAnalysisReport(Base):
    __tablename__ = "daily_analysis_reports"
    __table_args__ = (UniqueConstraint("report_date", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    version: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    report: Mapped[dict] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


def ensure_daily_analysis_report_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        DailyAnalysisReport.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def _previous_business_date() -> date:
    local_now = datetime.now(ZoneInfo(BUSINESS_TIMEZONE))
    return local_now.date() - timedelta(days=1)


def persist_daily_report(report_date: date, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_daily_analysis_report_schema()
    with SessionLocal() as session:
        existing = session.scalar(
            select(DailyAnalysisReport).where(
                DailyAnalysisReport.report_date == report_date,
                DailyAnalysisReport.version == DAILY_ANALYSIS_REPORT_VERSION,
            )
        )
        if existing is not None:
            return {"status": "exists", "report_id": int(existing.id), "report_date": report_date.isoformat()}
        row = DailyAnalysisReport(
            report_date=report_date,
            version=DAILY_ANALYSIS_REPORT_VERSION,
            status=str(payload.get("status") or "ok"),
            report=payload,
            generated_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"status": "persisted", "report_id": int(row.id), "report_date": report_date.isoformat()}


def latest_daily_report() -> dict[str, Any] | None:
    ensure_daily_analysis_report_schema()
    with SessionLocal() as session:
        row = session.scalar(
            select(DailyAnalysisReport)
            .order_by(DailyAnalysisReport.report_date.desc(), DailyAnalysisReport.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        return {
            "report_id": int(row.id),
            "report_date": row.report_date.isoformat(),
            "version": row.version,
            "status": row.status,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            "report": row.report,
        }


async def generate_daily_analysis_report(report_date: date | None = None) -> dict[str, Any]:
    target_date = report_date or _previous_business_date()
    settlement = await settle_pending_records(limit=25)
    report = build_forward_test_report_v3(
        start_date=target_date,
        end_date=target_date,
        max_records=5000,
    )
    report["daily_generation"] = {
        "version": DAILY_ANALYSIS_REPORT_VERSION,
        "target_date": target_date.isoformat(),
        "settlement_before_report": settlement,
        "generated_automatically": True,
    }
    stored = persist_daily_report(target_date, report)
    return {
        "status": "ok",
        "version": DAILY_ANALYSIS_REPORT_VERSION,
        "target_date": target_date.isoformat(),
        "settlement": settlement,
        "storage": stored,
        "report_overview": report.get("overview"),
    }


def main() -> None:
    result = asyncio.run(generate_daily_analysis_report())
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
