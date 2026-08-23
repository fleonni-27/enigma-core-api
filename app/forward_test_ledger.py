from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timezone
from decimal import Decimal
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Numeric, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine
from app.decision_engine import (
    CALIBRATION_ID,
    CALIBRATION_VERSION,
    DECISION_ENGINE_VERSION,
    DEFAULT_MAX_OVERROUND,
    DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_EXPECTED_VALUE,
    evaluate_fixture_decision,
)

FORWARD_TEST_LEDGER_VERSION = "forward_test_ledger_v1"
DEFAULT_MODEL_VERSION = "baseline_1x2_temporal_v1"
DEFAULT_PREDICTION_WINDOW = "prematch_v1"
MAX_LEDGER_ROWS = 500

router = APIRouter()

_schema_lock = Lock()
_schema_ready = False


class DecisionRecord(Base):
    __tablename__ = "decision_records"
    __table_args__ = (
        UniqueConstraint("record_key", name="uq_decision_records_record_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    record_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    sportmonks_fixture_id: Mapped[int] = mapped_column(BigInteger, index=True)
    league: Mapped[str | None] = mapped_column(String(160), index=True)
    home_team: Mapped[str] = mapped_column(String(160))
    away_team: Mapped[str] = mapped_column(String(160))
    fixture_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), index=True)
    prediction_window: Mapped[str] = mapped_column(String(30), index=True)
    model_version: Mapped[str] = mapped_column(String(50), index=True)

    decision_engine_version: Mapped[str] = mapped_column(String(50), index=True)
    calibration_version: Mapped[str] = mapped_column(String(80))
    calibration_id: Mapped[str] = mapped_column(String(120))
    snapshot_window: Mapped[str] = mapped_column(String(30), index=True)
    source: Mapped[str] = mapped_column(String(80), index=True)

    decision: Mapped[str] = mapped_column(String(12), index=True)
    selection: Mapped[str | None] = mapped_column(String(2))
    reason_codes: Mapped[list] = mapped_column(JSONB)

    raw_probabilities: Mapped[dict] = mapped_column(JSONB)
    raw_favorite_probability: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    calibrated_favorite_confidence: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))

    bookmaker: Mapped[str | None] = mapped_column(String(120), index=True)
    market_name: Mapped[str | None] = mapped_column(String(120))
    selected_odd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selected_no_vig_probability: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    overround: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latest_quote_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    edge_probability_points: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    edge_percentage_points: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    expected_value_decimal: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    expected_value_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))

    thresholds: Mapped[dict] = mapped_column(JSONB)
    market_scan: Mapped[dict] = mapped_column(JSONB)
    decision_payload: Mapped[dict] = mapped_column(JSONB)

    settlement_status: Mapped[str] = mapped_column(String(20), default="UNSETTLED", index=True)
    actual_result: Mapped[str | None] = mapped_column(String(2))
    selection_won: Mapped[str | None] = mapped_column(String(8))
    hypothetical_pnl_units: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    counterfactual_pnl_units: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


def ensure_forward_test_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        DecisionRecord.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _json_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _record_payload(record: DecisionRecord) -> dict[str, Any]:
    return {
        "record_id": int(record.id),
        "record_key": record.record_key,
        "fixture": {
            "fixture_id": int(record.fixture_id),
            "sportmonks_fixture_id": int(record.sportmonks_fixture_id),
            "league": record.league,
            "home_team": record.home_team,
            "away_team": record.away_team,
            "starts_at": record.fixture_starts_at.isoformat() if record.fixture_starts_at else None,
        },
        "prediction": {
            "prediction_id": int(record.prediction_id),
            "prediction_window": record.prediction_window,
            "model_version": record.model_version,
            "raw_probabilities": record.raw_probabilities,
            "raw_favorite_probability": float(record.raw_favorite_probability) if record.raw_favorite_probability is not None else None,
        },
        "decision": {
            "decision_engine_version": record.decision_engine_version,
            "calibration_version": record.calibration_version,
            "calibration_id": record.calibration_id,
            "snapshot_window": record.snapshot_window,
            "source": record.source,
            "decision": record.decision,
            "selection": record.selection,
            "reason_codes": record.reason_codes or [],
            "calibrated_favorite_confidence": float(record.calibrated_favorite_confidence) if record.calibrated_favorite_confidence is not None else None,
        },
        "market": {
            "bookmaker": record.bookmaker,
            "market_name": record.market_name,
            "selected_odd": float(record.selected_odd) if record.selected_odd is not None else None,
            "selected_no_vig_probability": float(record.selected_no_vig_probability) if record.selected_no_vig_probability is not None else None,
            "overround": float(record.overround) if record.overround is not None else None,
            "latest_quote_fetched_at": record.latest_quote_fetched_at.isoformat() if record.latest_quote_fetched_at else None,
        },
        "value": {
            "edge_probability_points": float(record.edge_probability_points) if record.edge_probability_points is not None else None,
            "edge_percentage_points": float(record.edge_percentage_points) if record.edge_percentage_points is not None else None,
            "expected_value_decimal": float(record.expected_value_decimal) if record.expected_value_decimal is not None else None,
            "expected_value_pct": float(record.expected_value_pct) if record.expected_value_pct is not None else None,
        },
        "thresholds": record.thresholds or {},
        "market_scan": record.market_scan or {},
        "settlement": {
            "status": record.settlement_status,
            "actual_result": record.actual_result,
            "selection_won": record.selection_won,
            "hypothetical_pnl_units": float(record.hypothetical_pnl_units) if record.hypothetical_pnl_units is not None else None,
            "counterfactual_pnl_units": float(record.counterfactual_pnl_units) if record.counterfactual_pnl_units is not None else None,
            "settled_at": record.settled_at.isoformat() if record.settled_at else None,
        },
        "recorded_at": record.recorded_at.isoformat() if record.recorded_at else None,
    }


def persist_evaluated_decision(
    decision_result: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    if decision_result.get("status") != "ok":
        return {
            "status": "not_persisted",
            "version": FORWARD_TEST_LEDGER_VERSION,
            "reason_codes": ["DECISION_NOT_READY"],
        }

    fixture = decision_result.get("fixture") or {}
    prediction = decision_result.get("prediction") or {}
    best_market = decision_result.get("best_market") or {}
    market = best_market.get("market") or {}
    value = best_market.get("value") or {}
    decision_model = decision_result.get("decision_model") or {}
    decision_calibration = decision_result.get("decision_calibration") or {}
    thresholds = best_market.get("thresholds") or {}
    market_scan = decision_result.get("market_scan") or {}

    fixture_starts_at = _parse_datetime(fixture.get("starts_at"))
    if fixture_starts_at is None:
        return {
            "status": "not_persisted",
            "version": FORWARD_TEST_LEDGER_VERSION,
            "reason_codes": ["FIXTURE_START_TIME_NOT_AVAILABLE"],
        }

    now = datetime.now(timezone.utc)
    if now >= fixture_starts_at:
        return {
            "status": "not_persisted",
            "version": FORWARD_TEST_LEDGER_VERSION,
            "reason_codes": ["FIXTURE_ALREADY_STARTED_FORWARD_RECORD_FORBIDDEN"],
            "fixture_starts_at": fixture_starts_at.isoformat(),
            "evaluated_at": now.isoformat(),
        }

    snapshot_window = best_market.get("snapshot_window")
    prediction_id = prediction.get("prediction_id")
    fixture_id = fixture.get("fixture_id")
    sportmonks_fixture_id = fixture.get("sportmonks_fixture_id")
    if not snapshot_window or prediction_id is None or fixture_id is None or sportmonks_fixture_id is None:
        return {
            "status": "not_persisted",
            "version": FORWARD_TEST_LEDGER_VERSION,
            "reason_codes": ["DECISION_AUDIT_IDENTIFIERS_INCOMPLETE"],
        }

    key_payload = {
        "fixture_id": fixture_id,
        "prediction_id": prediction_id,
        "prediction_window": prediction.get("prediction_window"),
        "model_version": prediction.get("model_version"),
        "decision_engine_version": decision_result.get("version"),
        "calibration_id": decision_calibration.get("calibration_id") or CALIBRATION_ID,
        "snapshot_window": snapshot_window,
        "bookmaker": best_market.get("bookmaker"),
        "market_name": best_market.get("market_name"),
        "selected_odd": market.get("selected_odd"),
        "latest_quote_fetched_at": best_market.get("latest_quote_fetched_at"),
        "decision": decision_result.get("decision"),
        "selection": decision_result.get("selection"),
        "reason_codes": decision_result.get("reason_codes") or [],
        "thresholds": thresholds,
    }
    record_key = _json_hash(key_payload)

    ensure_forward_test_schema()

    with SessionLocal() as session:
        existing = session.scalar(
            select(DecisionRecord).where(DecisionRecord.record_key == record_key)
        )
        if existing is not None:
            return {
                "status": "exists",
                "version": FORWARD_TEST_LEDGER_VERSION,
                "record": _record_payload(existing),
                "policy": {
                    "immutable_record": True,
                    "retroactive_forward_recording_allowed": False,
                },
            }

        record = DecisionRecord(
            record_key=record_key,
            fixture_id=int(fixture_id),
            sportmonks_fixture_id=int(sportmonks_fixture_id),
            league=fixture.get("league"),
            home_team=str(fixture.get("home_team") or "Unknown"),
            away_team=str(fixture.get("away_team") or "Unknown"),
            fixture_starts_at=fixture_starts_at,
            prediction_id=int(prediction_id),
            prediction_window=str(prediction.get("prediction_window") or ""),
            model_version=str(prediction.get("model_version") or ""),
            decision_engine_version=str(decision_result.get("version") or DECISION_ENGINE_VERSION),
            calibration_version=str(decision_calibration.get("version") or CALIBRATION_VERSION),
            calibration_id=str(decision_calibration.get("calibration_id") or CALIBRATION_ID),
            snapshot_window=str(snapshot_window),
            source=str(source)[:80],
            decision=str(decision_result.get("decision") or "NO_BET"),
            selection=decision_result.get("selection"),
            reason_codes=list(decision_result.get("reason_codes") or []),
            raw_probabilities=dict(decision_model.get("raw_probabilities") or {}),
            raw_favorite_probability=_decimal(decision_model.get("raw_favorite_probability")),
            calibrated_favorite_confidence=_decimal(decision_calibration.get("calibrated_favorite_confidence")),
            bookmaker=best_market.get("bookmaker"),
            market_name=best_market.get("market_name"),
            selected_odd=_decimal(market.get("selected_odd")),
            selected_no_vig_probability=_decimal(market.get("selected_no_vig_probability")),
            overround=_decimal(market.get("overround")),
            latest_quote_fetched_at=_parse_datetime(best_market.get("latest_quote_fetched_at")),
            edge_probability_points=_decimal(value.get("edge_probability_points")),
            edge_percentage_points=_decimal(value.get("edge_percentage_points")),
            expected_value_decimal=_decimal(value.get("expected_value_decimal")),
            expected_value_pct=_decimal(value.get("expected_value_pct")),
            thresholds=dict(thresholds),
            market_scan=dict(market_scan),
            decision_payload=decision_result,
            settlement_status="UNSETTLED",
        )
        session.add(record)
        try:
            session.commit()
            session.refresh(record)
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(DecisionRecord).where(DecisionRecord.record_key == record_key)
            )
            if existing is None:
                raise
            return {
                "status": "exists",
                "version": FORWARD_TEST_LEDGER_VERSION,
                "record": _record_payload(existing),
                "policy": {
                    "immutable_record": True,
                    "retroactive_forward_recording_allowed": False,
                },
            }

        return {
            "status": "persisted",
            "version": FORWARD_TEST_LEDGER_VERSION,
            "record": _record_payload(record),
            "policy": {
                "immutable_record": True,
                "retroactive_forward_recording_allowed": False,
                "execution_mode": "RESEARCH_ONLY",
                "real_money_execution_enabled": False,
                "settlement_status": "UNSETTLED",
            },
        }


def record_fixture_decision(
    *,
    sportmonks_fixture_id: int,
    prediction_window: str,
    model_version: str,
    snapshot_window: str,
    min_edge: float,
    min_expected_value: float,
    min_calibrated_confidence: float,
    max_overround: float,
    max_quote_span_seconds: int,
    require_team_favorite_top_class: bool,
    source: str = "manual_forward_test_record_v1",
) -> dict[str, Any]:
    decision = evaluate_fixture_decision(
        sportmonks_fixture_id=sportmonks_fixture_id,
        prediction_window=prediction_window,
        model_version=model_version,
        snapshot_window=snapshot_window,
        min_edge=min_edge,
        min_expected_value=min_expected_value,
        min_calibrated_confidence=min_calibrated_confidence,
        max_overround=max_overround,
        max_quote_span_seconds=max_quote_span_seconds,
        require_team_favorite_top_class=require_team_favorite_top_class,
        include_market_candidates=False,
    )
    persistence = persist_evaluated_decision(decision, source=source)
    return {
        "status": "ok" if persistence.get("status") in {"persisted", "exists"} else "not_ready",
        "version": FORWARD_TEST_LEDGER_VERSION,
        "decision": decision,
        "persistence": persistence,
    }


def list_forward_test_records(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    league: str | None = None,
    decision: str | None = None,
    settlement_status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if start_date and end_date and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_LEDGER_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_LEDGER_ROWS}")

    normalized_decision = decision.upper().strip() if decision else None
    if normalized_decision and normalized_decision not in {"BET", "NO_BET"}:
        raise ValueError("decision must be BET or NO_BET")

    normalized_settlement = settlement_status.upper().strip() if settlement_status else None
    if normalized_settlement and normalized_settlement not in {"UNSETTLED", "SETTLED"}:
        raise ValueError("settlement_status must be UNSETTLED or SETTLED")

    ensure_forward_test_schema()

    query = select(DecisionRecord)
    count_query = select(func.count(DecisionRecord.id))
    conditions = []
    if start_date:
        conditions.append(
            DecisionRecord.fixture_starts_at
            >= datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        )
    if end_date:
        conditions.append(
            DecisionRecord.fixture_starts_at
            <= datetime.combine(end_date, time.max, tzinfo=timezone.utc)
        )
    if league:
        conditions.append(DecisionRecord.league == league)
    if normalized_decision:
        conditions.append(DecisionRecord.decision == normalized_decision)
    if normalized_settlement:
        conditions.append(DecisionRecord.settlement_status == normalized_settlement)

    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    query = query.order_by(DecisionRecord.recorded_at.desc(), DecisionRecord.id.desc()).limit(limit)

    with SessionLocal() as session:
        total_matching = int(session.scalar(count_query) or 0)
        rows = session.scalars(query).all()

    reason_counts: Counter[str] = Counter()
    for row in rows:
        for reason in row.reason_codes or []:
            reason_counts[str(reason)] += 1

    return {
        "status": "ok",
        "version": FORWARD_TEST_LEDGER_VERSION,
        "filters": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "league": league,
            "decision": normalized_decision,
            "settlement_status": normalized_settlement,
            "limit": limit,
        },
        "total_matching": total_matching,
        "returned": len(rows),
        "returned_reason_code_counts": dict(sorted(reason_counts.items())),
        "records": [_record_payload(row) for row in rows],
        "policy": {
            "forward_records_are_immutable": True,
            "retroactive_forward_recording_allowed": False,
            "outcome_settlement_implemented": False,
        },
    }


def get_fixture_forward_test_records(
    sportmonks_fixture_id: int,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_LEDGER_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_LEDGER_ROWS}")
    ensure_forward_test_schema()
    with SessionLocal() as session:
        rows = session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.sportmonks_fixture_id == sportmonks_fixture_id)
            .order_by(DecisionRecord.recorded_at.desc(), DecisionRecord.id.desc())
            .limit(limit)
        ).all()
    return {
        "status": "ok",
        "version": FORWARD_TEST_LEDGER_VERSION,
        "sportmonks_fixture_id": sportmonks_fixture_id,
        "record_count": len(rows),
        "records": [_record_payload(row) for row in rows],
    }


@router.post("/research/forward-test/record/fixture/{sportmonks_fixture_id}")
def record_fixture_decision_endpoint(
    sportmonks_fixture_id: int,
    prediction_window: str = Query(default=DEFAULT_PREDICTION_WINDOW, min_length=1, max_length=30),
    model_version: str = Query(default=DEFAULT_MODEL_VERSION, min_length=1, max_length=50),
    snapshot_window: str = Query(..., min_length=1, max_length=30),
    min_edge: float = Query(default=DEFAULT_MIN_EDGE, ge=0.0, le=0.30),
    min_expected_value: float = Query(default=DEFAULT_MIN_EXPECTED_VALUE, ge=0.0, le=0.50),
    min_calibrated_confidence: float = Query(default=DEFAULT_MIN_CALIBRATED_CONFIDENCE, ge=0.30, le=0.80),
    max_overround: float = Query(default=DEFAULT_MAX_OVERROUND, ge=0.0, le=0.30),
    max_quote_span_seconds: int = Query(default=DEFAULT_MAX_QUOTE_SPAN_SECONDS, ge=0, le=3600),
    require_team_favorite_top_class: bool = True,
) -> dict[str, Any]:
    try:
        return record_fixture_decision(
            sportmonks_fixture_id=sportmonks_fixture_id,
            prediction_window=prediction_window,
            model_version=model_version,
            snapshot_window=snapshot_window,
            min_edge=min_edge,
            min_expected_value=min_expected_value,
            min_calibrated_confidence=min_calibrated_confidence,
            max_overround=max_overround,
            max_quote_span_seconds=max_quote_span_seconds,
            require_team_favorite_top_class=require_team_favorite_top_class,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


@router.get("/research/forward-test/ledger")
def forward_test_ledger_endpoint(
    start_date: date | None = None,
    end_date: date | None = None,
    league: str | None = Query(default=None, max_length=160),
    decision: str | None = Query(default=None, max_length=12),
    settlement_status: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=100, ge=1, le=MAX_LEDGER_ROWS),
) -> dict[str, Any]:
    try:
        return list_forward_test_records(
            start_date=start_date,
            end_date=end_date,
            league=league,
            decision=decision,
            settlement_status=settlement_status,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


@router.get("/research/forward-test/fixture/{sportmonks_fixture_id}")
def forward_test_fixture_endpoint(
    sportmonks_fixture_id: int,
    limit: int = Query(default=100, ge=1, le=MAX_LEDGER_ROWS),
) -> dict[str, Any]:
    try:
        return get_fixture_forward_test_records(
            sportmonks_fixture_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
