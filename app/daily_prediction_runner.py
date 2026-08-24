from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal, engine
from app.decision_engine import (
    DEFAULT_MAX_OVERROUND,
    DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_EXPECTED_VALUE,
    evaluate_fixture_decision,
)
from app.forward_test_ledger import (
    DecisionRecord,
    ensure_forward_test_schema,
    persist_evaluated_decision,
)
from app.league_registry import canonical_league
from app.models import Fixture
from app.odds_ingestion import ingest_prematch_odds_payload
from app.prematch_inference import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_LOOKBACK_MATCHES,
    DEFAULT_MAX_TRAINING_ROWS,
    DEFAULT_MIN_HISTORY_MATCHES,
    DEFAULT_MIN_TRAINING_ROWS,
    MODEL_VERSION,
    generate_and_persist_prematch_prediction,
)
from app.sportmonks import SportmonksClient

DAILY_PREDICTION_RUNNER_VERSION = "daily_prediction_runner_v1"
BUSINESS_TIMEZONE = "America/Sao_Paulo"
J1_TARGET_LEAD_MINUTES = 45
J1_PREDICTION_WINDOW = "j1_45m_v1"
J1_SNAPSHOT_PREFIX = "j1_45m"
DEFAULT_MAX_LATENESS_MINUTES = 20
DEFAULT_MAX_FIXTURES = 5
MAX_FIXTURES_PER_RUN = 5

router = APIRouter(prefix="/operations", tags=["operations"])

_schema_lock = Lock()
_schema_ready = False


class PrematchContextSnapshot(Base):
    __tablename__ = "prematch_context_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    sportmonks_fixture_id: Mapped[int] = mapped_column(BigInteger, index=True)
    snapshot_window: Mapped[str] = mapped_column(String(30), index=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    lineups: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    lineup_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


def ensure_prematch_context_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        PrematchContextSnapshot.__table__.create(bind=engine, checkfirst=True)
        _schema_ready = True


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _business_today() -> date:
    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()


def _utc_bounds(target_date: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    local_start = datetime.combine(target_date, time.min, tzinfo=tz)
    local_end = datetime.combine(target_date, time.max, tzinfo=tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _snapshot_window(fixture: Fixture) -> str:
    local_date = _aware_utc(fixture.starts_at).astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date()
    return f"{J1_SNAPSHOT_PREFIX}_{local_date.strftime('%Y%m%d')}"


def _fixture_payload(fixture: Fixture, now: datetime) -> dict[str, Any]:
    starts_at = _aware_utc(fixture.starts_at)
    due_at = starts_at - timedelta(minutes=J1_TARGET_LEAD_MINUTES)
    canonical = canonical_league(fixture.league_name)
    return {
        "fixture_id": int(fixture.id),
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "league": canonical.get("canonical_name") or fixture.league_name,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "starts_at": starts_at.isoformat(),
        "j1_due_at": due_at.isoformat(),
        "minutes_to_kickoff": round((starts_at - now).total_seconds() / 60.0, 2),
        "minutes_after_j1_due": round(max(0.0, (now - due_at).total_seconds() / 60.0), 2),
        "status": fixture.status,
    }


def _due_target_fixtures(
    *,
    now: datetime,
    max_lateness_minutes: int,
    max_fixtures: int,
) -> list[Fixture]:
    # J1 is never executed early. A fixture becomes due exactly at kickoff-45m
    # and remains eligible for a bounded grace period so scheduler delays do not
    # make the forward record disappear.
    latest_kickoff = now + timedelta(minutes=J1_TARGET_LEAD_MINUTES)
    earliest_kickoff = now + timedelta(
        minutes=max(1, J1_TARGET_LEAD_MINUTES - max_lateness_minutes)
    )

    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at >= earliest_kickoff,
                Fixture.starts_at <= latest_kickoff,
                Fixture.starts_at > now,
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    result: list[Fixture] = []
    for fixture in candidates:
        canonical = canonical_league(fixture.league_name)
        if not canonical.get("target") or not canonical.get("key"):
            continue
        result.append(fixture)
        if len(result) >= max_fixtures:
            break
    return result


def _decision_already_recorded(fixture: Fixture, snapshot_window: str) -> bool:
    ensure_forward_test_schema()
    with SessionLocal() as session:
        existing = session.scalar(
            select(DecisionRecord.id)
            .where(
                DecisionRecord.fixture_id == fixture.id,
                DecisionRecord.snapshot_window == snapshot_window,
                DecisionRecord.source == DAILY_PREDICTION_RUNNER_VERSION,
            )
            .limit(1)
        )
    return existing is not None


def _persist_lineup_context(
    *,
    fixture: Fixture,
    snapshot_window: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ensure_prematch_context_schema()
    raw = payload.get("data") or {}
    lineups = raw.get("lineups") or []
    if not isinstance(lineups, list):
        lineups = []

    with SessionLocal() as session:
        snapshot = PrematchContextSnapshot(
            fixture_id=int(fixture.id),
            sportmonks_fixture_id=int(fixture.sportmonks_id),
            snapshot_window=snapshot_window,
            source="sportmonks_j1_45m",
            lineups=lineups,
            lineup_count=len(lineups),
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)

    return {
        "status": "ok",
        "prematch_context_snapshot_id": int(snapshot.id),
        "lineup_count": len(lineups),
        "lineups_available": len(lineups) > 0,
        "used_by_current_model": False,
    }


def _compact_decision(result: dict[str, Any]) -> dict[str, Any]:
    best_market = result.get("best_market") or {}
    market = best_market.get("market") or {}
    value = best_market.get("value") or {}
    calibration = result.get("decision_calibration") or {}
    return {
        "status": result.get("status"),
        "decision": result.get("decision"),
        "selection": result.get("selection"),
        "reason_codes": list(result.get("reason_codes") or []),
        "calibrated_favorite_confidence": calibration.get("calibrated_favorite_confidence"),
        "bookmaker": best_market.get("bookmaker"),
        "market_name": best_market.get("market_name"),
        "selected_odd": market.get("selected_odd"),
        "selected_no_vig_probability": market.get("selected_no_vig_probability"),
        "edge_percentage_points": value.get("edge_percentage_points"),
        "expected_value_pct": value.get("expected_value_pct"),
    }


async def run_daily_prediction_runner(
    *,
    max_lateness_minutes: int = DEFAULT_MAX_LATENESS_MINUTES,
    max_fixtures: int = DEFAULT_MAX_FIXTURES,
) -> dict[str, Any]:
    if max_lateness_minutes < 1 or max_lateness_minutes > 30:
        raise ValueError("max_lateness_minutes must be between 1 and 30")
    if max_fixtures < 1 or max_fixtures > MAX_FIXTURES_PER_RUN:
        raise ValueError(f"max_fixtures must be between 1 and {MAX_FIXTURES_PER_RUN}")

    now = datetime.now(timezone.utc)
    fixtures = _due_target_fixtures(
        now=now,
        max_lateness_minutes=max_lateness_minutes,
        max_fixtures=max_fixtures,
    )
    client = SportmonksClient()
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    for fixture in fixtures:
        snapshot_window = _snapshot_window(fixture)
        fixture_data = _fixture_payload(fixture, now)
        item: dict[str, Any] = {
            "fixture": fixture_data,
            "snapshot_window": snapshot_window,
            "lineup_context": None,
            "odds": None,
            "inference": None,
            "decision": None,
            "ledger": None,
        }

        if _decision_already_recorded(fixture, snapshot_window):
            counts["already_recorded"] += 1
            item["status"] = "already_recorded"
            items.append(item)
            continue

        try:
            enriched = await client.enriched_fixture(int(fixture.sportmonks_id))
            item["lineup_context"] = _persist_lineup_context(
                fixture=fixture,
                snapshot_window=snapshot_window,
                payload=enriched,
            )
            if item["lineup_context"].get("lineups_available"):
                counts["lineups_available"] += 1
            else:
                counts["lineups_not_available"] += 1
        except Exception as exc:
            counts["lineup_fetch_failed"] += 1
            item["lineup_context"] = {
                "status": "upstream_failed",
                "error": exc.__class__.__name__,
                "used_by_current_model": False,
            }

        try:
            odds_payload = await client.prematch_odds_by_fixture(int(fixture.sportmonks_id))
            odds_result = ingest_prematch_odds_payload(
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                payload=odds_payload,
                snapshot_window=snapshot_window,
            )
            item["odds"] = {
                "status": odds_result.get("status"),
                "received": odds_result.get("received", 0),
                "created": odds_result.get("created", 0),
                "filtered_out": odds_result.get("filtered_out", 0),
                "skipped": odds_result.get("skipped", 0),
                "error_count": len(odds_result.get("errors") or []),
            }
            counts["odds_rows_created"] += int(odds_result.get("created") or 0)
        except Exception as exc:
            counts["odds_failed"] += 1
            item["odds"] = {"status": "upstream_failed", "error": exc.__class__.__name__}

        try:
            inference = generate_and_persist_prematch_prediction(
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                prediction_window=J1_PREDICTION_WINDOW,
                history_days=DEFAULT_HISTORY_DAYS,
                lookback_matches=DEFAULT_LOOKBACK_MATCHES,
                min_history_matches=DEFAULT_MIN_HISTORY_MATCHES,
                min_training_rows=DEFAULT_MIN_TRAINING_ROWS,
                max_training_rows=DEFAULT_MAX_TRAINING_ROWS,
                class_weight_balanced=False,
            )
            item["inference"] = {
                "status": inference.get("status"),
                "reason_codes": list(inference.get("reason_codes") or []),
                "prediction": inference.get("prediction"),
                "target_feature_audit": inference.get("target_feature_audit"),
                "training_audit": inference.get("training_audit"),
            }
            if inference.get("status") not in {"ok", "exists"}:
                counts["inference_not_ready"] += 1
                item["status"] = "inference_not_ready"
                items.append(item)
                continue
            counts["inference_ready"] += 1
        except Exception as exc:
            counts["inference_failed"] += 1
            item["inference"] = {"status": "failed", "error": exc.__class__.__name__}
            item["status"] = "inference_failed"
            items.append(item)
            continue

        try:
            decision = evaluate_fixture_decision(
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                prediction_window=J1_PREDICTION_WINDOW,
                model_version=MODEL_VERSION,
                snapshot_window=snapshot_window,
                min_edge=DEFAULT_MIN_EDGE,
                min_expected_value=DEFAULT_MIN_EXPECTED_VALUE,
                min_calibrated_confidence=DEFAULT_MIN_CALIBRATED_CONFIDENCE,
                max_overround=DEFAULT_MAX_OVERROUND,
                max_quote_span_seconds=DEFAULT_MAX_QUOTE_SPAN_SECONDS,
                require_team_favorite_top_class=True,
                include_market_candidates=False,
            )
            item["decision"] = _compact_decision(decision)
            if decision.get("status") != "ok":
                counts["decision_not_ready"] += 1
                item["status"] = "decision_not_ready"
                items.append(item)
                continue

            counts["decisions_evaluated"] += 1
            if decision.get("decision") == "BET":
                counts["bet"] += 1
            elif decision.get("decision") == "NO_BET":
                counts["no_bet"] += 1

            ledger = persist_evaluated_decision(
                decision,
                source=DAILY_PREDICTION_RUNNER_VERSION,
            )
            item["ledger"] = {
                "status": ledger.get("status"),
                "reason_codes": list(ledger.get("reason_codes") or []),
                "record": ledger.get("record"),
            }
            if ledger.get("status") in {"persisted", "exists"}:
                counts["ledger_ready"] += 1
                item["status"] = "completed"
            else:
                counts["ledger_not_ready"] += 1
                item["status"] = "ledger_not_ready"
        except Exception as exc:
            counts["decision_failed"] += 1
            item["decision"] = {"status": "failed", "error": exc.__class__.__name__}
            item["status"] = "decision_failed"

        items.append(item)

    return {
        "status": "ok",
        "version": DAILY_PREDICTION_RUNNER_VERSION,
        "evaluated_at": now.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "window": {
            "name": "J1",
            "target_lead_minutes": J1_TARGET_LEAD_MINUTES,
            "execution_rule": "never early; first scheduler run at or after kickoff-45m",
            "max_lateness_minutes": max_lateness_minutes,
            "prediction_window": J1_PREDICTION_WINDOW,
        },
        "selected_fixtures": len(fixtures),
        "counts": dict(counts),
        "items": items,
        "policy": {
            "target_leagues_only": True,
            "prediction_is_immutable_once_persisted": True,
            "decision_is_persisted_pre_kickoff": True,
            "odds_snapshot_is_j1_specific": True,
            "lineups_are_captured_in_separate_prematch_context_table": True,
            "pregame_lineups_do_not_pollute_postgame_training_snapshots": True,
            "lineups_used_by_current_standard_model": False,
            "current_standard_model_remains_36_features": True,
            "research_only": True,
            "auto_betting": False,
        },
    }


def build_j1_status(*, target_date: date | None = None) -> dict[str, Any]:
    ensure_forward_test_schema()
    ensure_prematch_context_schema()
    effective_date = target_date or _business_today()
    start_dt, end_dt = _utc_bounds(effective_date)
    now = datetime.now(timezone.utc)

    with SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

        items: list[dict[str, Any]] = []
        for fixture in fixtures:
            canonical = canonical_league(fixture.league_name)
            if not canonical.get("target") or not canonical.get("key"):
                continue
            window = _snapshot_window(fixture)
            decision_count = int(
                session.scalar(
                    select(func.count(DecisionRecord.id)).where(
                        DecisionRecord.fixture_id == fixture.id,
                        DecisionRecord.snapshot_window == window,
                        DecisionRecord.source == DAILY_PREDICTION_RUNNER_VERSION,
                    )
                )
                or 0
            )
            latest_context = session.scalar(
                select(PrematchContextSnapshot)
                .where(
                    PrematchContextSnapshot.fixture_id == fixture.id,
                    PrematchContextSnapshot.snapshot_window == window,
                )
                .order_by(PrematchContextSnapshot.fetched_at.desc(), PrematchContextSnapshot.id.desc())
                .limit(1)
            )
            item = _fixture_payload(fixture, now)
            item.update(
                {
                    "snapshot_window": window,
                    "j1_decision_recorded": decision_count > 0,
                    "lineups_captured": bool(latest_context and latest_context.lineup_count > 0),
                    "latest_lineup_count": int(latest_context.lineup_count) if latest_context else 0,
                    "latest_context_fetched_at": latest_context.fetched_at.isoformat()
                    if latest_context and latest_context.fetched_at
                    else None,
                }
            )
            items.append(item)

    return {
        "status": "ok",
        "version": DAILY_PREDICTION_RUNNER_VERSION,
        "target_date": effective_date.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "j1_target_lead_minutes": J1_TARGET_LEAD_MINUTES,
        "fixtures": items,
    }


@router.post("/daily-prediction-runner")
async def daily_prediction_runner_endpoint(
    max_lateness_minutes: int = Query(default=DEFAULT_MAX_LATENESS_MINUTES, ge=1, le=30),
    max_fixtures: int = Query(default=DEFAULT_MAX_FIXTURES, ge=1, le=MAX_FIXTURES_PER_RUN),
) -> dict[str, Any]:
    try:
        return await run_daily_prediction_runner(
            max_lateness_minutes=max_lateness_minutes,
            max_fixtures=max_fixtures,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


@router.get("/daily-prediction-runner/status")
def daily_prediction_runner_status_endpoint(
    target_date: date | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return build_j1_status(target_date=target_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
