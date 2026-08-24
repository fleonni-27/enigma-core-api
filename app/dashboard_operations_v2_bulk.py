from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select

from app import dashboard_operations_v2 as legacy
from app.daily_prediction_runner import (
    BUSINESS_TIMEZONE,
    DAILY_PREDICTION_RUNNER_VERSION,
    J1_PREDICTION_WINDOW,
    J1_TARGET_LEAD_MINUTES,
    PrematchContextSnapshot,
    ensure_prematch_context_schema,
)
from app.database import SessionLocal
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.league_registry import canonical_league
from app.models import Fixture, OddsSnapshot, Prediction

DASHBOARD_OPERATIONS_V2_BULK_VERSION = "dashboard_operations_v2_bulk_reads_v1"


def _latest_by_fixture(rows: list[Any]) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for row in rows:
        fixture_id = int(row.fixture_id)
        result.setdefault(fixture_id, row)
    return result


def _load_bulk_state(
    session,
    *,
    fixture_ids: list[int],
    snapshot_window: str,
) -> tuple[
    dict[int, tuple[int, datetime | None, int, datetime | None]],
    dict[int, PrematchContextSnapshot],
    dict[int, Prediction],
    dict[int, DecisionRecord],
]:
    """Load all dashboard state with four fixed queries, never per fixture."""

    if not fixture_ids:
        return {}, {}, {}, {}

    j1_count_expr = func.sum(
        case((OddsSnapshot.snapshot_window == snapshot_window, 1), else_=0)
    )
    j1_latest_expr = func.max(
        case(
            (OddsSnapshot.snapshot_window == snapshot_window, OddsSnapshot.fetched_at),
            else_=None,
        )
    )
    odds_rows = session.execute(
        select(
            OddsSnapshot.fixture_id,
            func.count(OddsSnapshot.id),
            func.max(OddsSnapshot.fetched_at),
            j1_count_expr,
            j1_latest_expr,
        )
        .where(OddsSnapshot.fixture_id.in_(fixture_ids))
        .group_by(OddsSnapshot.fixture_id)
    ).all()
    odds_by_fixture = {
        int(fixture_id): (
            int(total_count or 0),
            latest_at,
            int(j1_count or 0),
            latest_j1_at,
        )
        for fixture_id, total_count, latest_at, j1_count, latest_j1_at in odds_rows
    }

    contexts = session.scalars(
        select(PrematchContextSnapshot)
        .where(
            PrematchContextSnapshot.fixture_id.in_(fixture_ids),
            PrematchContextSnapshot.snapshot_window == snapshot_window,
        )
        .order_by(
            PrematchContextSnapshot.fixture_id.asc(),
            PrematchContextSnapshot.fetched_at.desc(),
            PrematchContextSnapshot.id.desc(),
        )
    ).all()
    contexts_by_fixture = _latest_by_fixture(list(contexts))

    predictions = session.scalars(
        select(Prediction)
        .where(
            Prediction.fixture_id.in_(fixture_ids),
            Prediction.prediction_window == J1_PREDICTION_WINDOW,
        )
        .order_by(
            Prediction.fixture_id.asc(),
            Prediction.generated_at.desc(),
            Prediction.id.desc(),
        )
    ).all()
    predictions_by_fixture = _latest_by_fixture(list(predictions))

    decisions = session.scalars(
        select(DecisionRecord)
        .where(
            DecisionRecord.fixture_id.in_(fixture_ids),
            DecisionRecord.snapshot_window == snapshot_window,
            DecisionRecord.source == DAILY_PREDICTION_RUNNER_VERSION,
        )
        .order_by(
            DecisionRecord.fixture_id.asc(),
            DecisionRecord.recorded_at.desc(),
            DecisionRecord.id.desc(),
        )
    ).all()
    decisions_by_fixture = _latest_by_fixture(list(decisions))

    return (
        odds_by_fixture,
        contexts_by_fixture,
        predictions_by_fixture,
        decisions_by_fixture,
    )


def build_dashboard_operations_v2_bulk(*, target_date: date | None = None) -> dict[str, Any]:
    ensure_forward_test_schema()
    ensure_prematch_context_schema()

    effective_date = target_date or legacy._business_today()
    start_dt, end_dt = legacy._utc_bounds(effective_date)
    now = datetime.now(timezone.utc)
    window = legacy._snapshot_window(effective_date)

    with SessionLocal() as session:
        all_fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

        fixtures: list[Fixture] = []
        canonical_by_fixture: dict[int, dict[str, Any]] = {}
        for fixture in all_fixtures:
            canonical = canonical_league(fixture.league_name)
            if canonical.get("target") and canonical.get("key"):
                fixtures.append(fixture)
                canonical_by_fixture[int(fixture.id)] = canonical

        fixture_ids = [int(fixture.id) for fixture in fixtures]
        (
            odds_by_fixture,
            contexts_by_fixture,
            predictions_by_fixture,
            decisions_by_fixture,
        ) = _load_bulk_state(
            session,
            fixture_ids=fixture_ids,
            snapshot_window=window,
        )

        items: list[dict[str, Any]] = []
        stages: Counter[str] = Counter()

        for fixture in fixtures:
            fixture_id = int(fixture.id)
            starts_at = legacy._aware_utc(fixture.starts_at)
            j1_due_at = starts_at - timedelta(minutes=J1_TARGET_LEAD_MINUTES)

            (
                daily_odds_rows,
                latest_daily_odds_at,
                j1_odds_rows,
                latest_j1_odds_at,
            ) = odds_by_fixture.get(fixture_id, (0, None, 0, None))
            context = contexts_by_fixture.get(fixture_id)
            prediction = predictions_by_fixture.get(fixture_id)
            decision = decisions_by_fixture.get(fixture_id)

            stage = legacy._stage(
                now=now,
                starts_at=starts_at,
                j1_due_at=j1_due_at,
                has_decision=decision is not None,
                has_prediction=prediction is not None,
                has_j1_odds=j1_odds_rows > 0,
                has_context_snapshot=context is not None,
            )
            stages[stage] += 1

            probabilities = None
            if prediction is not None:
                probabilities = {
                    "home": legacy._f(prediction.p_home),
                    "draw": legacy._f(prediction.p_draw),
                    "away": legacy._f(prediction.p_away),
                }
            elif decision is not None:
                raw = decision.raw_probabilities or {}
                probabilities = {
                    "home": legacy._f(raw.get("1")),
                    "draw": legacy._f(raw.get("X")),
                    "away": legacy._f(raw.get("2")),
                }

            decision_payload = None
            if decision is not None:
                decision_payload = {
                    "record_id": int(decision.id),
                    "decision": decision.decision,
                    "selection": decision.selection,
                    "reason_codes": list(decision.reason_codes or []),
                    "bookmaker": decision.bookmaker,
                    "selected_odd": legacy._f(decision.selected_odd),
                    "selected_no_vig_probability": legacy._f(
                        decision.selected_no_vig_probability
                    ),
                    "calibrated_confidence": legacy._f(
                        decision.calibrated_favorite_confidence
                    ),
                    "edge_pct": legacy._f(decision.edge_percentage_points),
                    "expected_value_pct": legacy._f(decision.expected_value_pct),
                    "recorded_at": decision.recorded_at.isoformat()
                    if decision.recorded_at
                    else None,
                    "settlement_status": decision.settlement_status,
                }

            canonical = canonical_by_fixture[fixture_id]
            items.append(
                {
                    "fixture_id": fixture_id,
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "league": canonical.get("canonical_name") or fixture.league_name,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "starts_at": starts_at.isoformat(),
                    "starts_at_local": starts_at.astimezone(
                        ZoneInfo(BUSINESS_TIMEZONE)
                    ).isoformat(),
                    "j1_due_at": j1_due_at.isoformat(),
                    "j1_due_at_local": j1_due_at.astimezone(
                        ZoneInfo(BUSINESS_TIMEZONE)
                    ).isoformat(),
                    "minutes_to_kickoff": round(
                        (starts_at - now).total_seconds() / 60.0, 2
                    ),
                    "minutes_to_j1": round(
                        (j1_due_at - now).total_seconds() / 60.0, 2
                    ),
                    "stage": stage,
                    "snapshot_window": window,
                    "steps": {
                        "fixture": {"status": "READY"},
                        "daily_odds": {
                            "status": "READY" if daily_odds_rows > 0 else "MISSING",
                            "rows": daily_odds_rows,
                            "latest_fetched_at": latest_daily_odds_at.isoformat()
                            if latest_daily_odds_at
                            else None,
                        },
                        "lineups": {
                            "status": (
                                "READY"
                                if context is not None and context.lineup_count > 0
                                else "NOT_AVAILABLE"
                                if context is not None
                                else "WAITING"
                            ),
                            "count": int(context.lineup_count) if context else 0,
                            "latest_fetched_at": context.fetched_at.isoformat()
                            if context and context.fetched_at
                            else None,
                        },
                        "j1_odds": {
                            "status": "READY" if j1_odds_rows > 0 else "WAITING",
                            "rows": j1_odds_rows,
                            "latest_fetched_at": latest_j1_odds_at.isoformat()
                            if latest_j1_odds_at
                            else None,
                        },
                        "prediction": {
                            "status": "READY" if prediction is not None else "WAITING",
                            "prediction_id": int(prediction.id) if prediction else None,
                            "prediction_window": prediction.prediction_window
                            if prediction
                            else J1_PREDICTION_WINDOW,
                            "generated_at": prediction.generated_at.isoformat()
                            if prediction and prediction.generated_at
                            else None,
                        },
                        "decision": {
                            "status": "READY" if decision is not None else "WAITING"
                        },
                        "ledger": {
                            "status": "READY" if decision is not None else "WAITING",
                            "record_id": int(decision.id) if decision else None,
                        },
                    },
                    "probabilities": probabilities,
                    "decision": decision_payload,
                }
            )

    future_due = [
        item for item in items if datetime.fromisoformat(item["j1_due_at"]) >= now
    ]
    next_j1 = min(future_due, key=lambda item: item["j1_due_at"]) if future_due else None

    return {
        "status": "ok",
        "version": legacy.DASHBOARD_OPERATIONS_V2_VERSION,
        "generated_at": now.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "target_date": effective_date.isoformat(),
        "snapshot_window": window,
        "overview": {
            "target_fixtures": len(items),
            "j1_complete": stages["J1_COMPLETE"],
            "waiting_j1": stages["WAITING_J1"],
            "j1_due_or_processing": sum(
                stages[key]
                for key in (
                    "J1_DUE",
                    "PROCESSING_J1",
                    "PROCESSING_PREDICTION",
                    "PROCESSING_DECISION",
                )
            ),
            "attention": stages["J1_WINDOW_MISSED"]
            + stages["J1_NOT_RECORDED_BEFORE_KICKOFF"],
            "next_j1": {
                "sportmonks_fixture_id": next_j1["sportmonks_fixture_id"],
                "match": f"{next_j1['home_team']} x {next_j1['away_team']}",
                "due_at_local": next_j1["j1_due_at_local"],
            }
            if next_j1
            else None,
        },
        "stage_counts": dict(stages),
        "fixtures": items,
        "performance": {
            "version": DASHBOARD_OPERATIONS_V2_BULK_VERSION,
            "query_strategy": "fixed_bulk_reads",
            "fixture_query_count": 1,
            "bulk_state_query_count": 4 if fixture_ids else 0,
            "per_fixture_query_count": 0,
            "data_select_query_count": 5 if fixture_ids else 1,
            "query_count_scales_with_fixture_count": False,
        },
        "policy": {
            "read_only": True,
            "auto_refresh_seconds": 60,
            "j1_target_lead_minutes": J1_TARGET_LEAD_MINUTES,
            "j1_max_lateness_minutes": legacy.DEFAULT_MAX_LATENESS_MINUTES,
            "lineups_used_by_current_model": False,
            "research_only": True,
            "real_money_execution_enabled": False,
        },
    }


def install_dashboard_operations_v2_bulk_reads() -> None:
    legacy.build_dashboard_operations_v2 = build_dashboard_operations_v2_bulk
