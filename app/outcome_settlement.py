from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.database import SessionLocal
from app.forward_test_ledger import (
    DecisionRecord,
    _record_payload,
    ensure_forward_test_schema,
    router as forward_test_router,
)

OUTCOME_SETTLEMENT_VERSION = "outcome_settlement_v1"
MAX_PENDING_FIXTURES_PER_RUN = 25
FINISHED_STATE_IDS = {5, 7, 8}  # FT, AET, FT_PEN
FINISHED_STATE_CODES = {"FT", "AET", "FT_PEN"}

router = APIRouter()
_routes_installed = False


def _normalize_state_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _score_pair(scores: list[dict[str, Any]], description: str) -> tuple[int, int] | None:
    target = description.strip().upper()
    values: dict[str, int] = {}
    for row in scores:
        if str(row.get("description") or "").strip().upper() != target:
            continue
        score = row.get("score") or {}
        participant = str(score.get("participant") or "").strip().lower()
        goals = _coerce_int(score.get("goals"))
        if participant in {"home", "away"} and goals is not None:
            values[participant] = goals
    if "home" not in values or "away" not in values:
        return None
    return values["home"], values["away"]


def _parse_fixture_outcome(payload: dict[str, Any], expected_fixture_id: int) -> dict[str, Any]:
    data = payload.get("data") or {}
    returned_fixture_id = _coerce_int(data.get("id"))
    if returned_fixture_id is None:
        return {
            "status": "not_ready",
            "reason_codes": ["UPSTREAM_FIXTURE_ID_NOT_AVAILABLE"],
        }
    if returned_fixture_id != expected_fixture_id:
        return {
            "status": "not_ready",
            "reason_codes": ["UPSTREAM_FIXTURE_ID_MISMATCH"],
            "returned_fixture_id": returned_fixture_id,
        }

    state = data.get("state") or {}
    state_id = _coerce_int(data.get("state_id")) or _coerce_int(state.get("id"))
    state_code = _normalize_state_code(
        state.get("state") or state.get("short_name") or state.get("developer_name")
    )

    finished = state_id in FINISHED_STATE_IDS or state_code in FINISHED_STATE_CODES
    if not finished:
        return {
            "status": "not_ready",
            "reason_codes": ["FIXTURE_NOT_FINISHED"],
            "state": {
                "id": state_id,
                "code": state_code or None,
                "name": state.get("name"),
            },
        }

    scores = data.get("scores") or []

    # Fulltime Result / 1X2 is a 90-minute market. For fixtures that later
    # continue into extra time or penalties, Sportmonks' 2ND_HALF score is the
    # cumulative score after the two regulation halves, so it is preferred.
    score_pair = _score_pair(scores, "2ND_HALF")
    score_source = "2ND_HALF"

    # A regular FT fixture may occasionally expose only CURRENT. CURRENT is a
    # safe fallback only for state FT; for AET/FT_PEN it can contain extra-time
    # or shootout information and must not be used for 90-minute settlement.
    if score_pair is None and (state_id == 5 or state_code == "FT"):
        score_pair = _score_pair(scores, "CURRENT")
        score_source = "CURRENT"

    if score_pair is None:
        return {
            "status": "not_ready",
            "reason_codes": ["REGULATION_SCORE_NOT_AVAILABLE"],
            "state": {
                "id": state_id,
                "code": state_code or None,
                "name": state.get("name"),
            },
        }

    home_goals, away_goals = score_pair
    if home_goals > away_goals:
        actual_result = "1"
    elif home_goals < away_goals:
        actual_result = "2"
    else:
        actual_result = "X"

    return {
        "status": "ok",
        "actual_result": actual_result,
        "regulation_score": {
            "home": home_goals,
            "away": away_goals,
            "source": score_source,
        },
        "state": {
            "id": state_id,
            "code": state_code or None,
            "name": state.get("name"),
        },
        "result_info": data.get("result_info"),
    }


def _selection_won(selection: str | None, actual_result: str) -> bool | None:
    if selection not in {"1", "X", "2"}:
        return None
    return selection == actual_result


def _counterfactual_pnl(selected_odd: Decimal | None, won: bool | None) -> Decimal | None:
    if selected_odd is None or won is None:
        return None
    if won:
        return selected_odd - Decimal("1")
    return Decimal("-1")


def _hypothetical_policy_pnl(
    decision: str,
    selected_odd: Decimal | None,
    won: bool | None,
) -> Decimal | None:
    normalized = str(decision or "").strip().upper()
    if normalized == "NO_BET":
        return Decimal("0")
    if normalized != "BET":
        return None
    return _counterfactual_pnl(selected_odd, won)


def _settlement_payload(record: DecisionRecord) -> dict[str, Any]:
    payload = _record_payload(record)
    settlement = payload.get("settlement") or {}
    raw_won = settlement.get("selection_won")
    if raw_won == "true":
        settlement["selection_won"] = True
    elif raw_won == "false":
        settlement["selection_won"] = False
    payload["settlement"] = settlement
    return payload


async def settle_fixture_records(sportmonks_fixture_id: int) -> dict[str, Any]:
    ensure_forward_test_schema()

    with SessionLocal() as session:
        existing_rows = session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.sportmonks_fixture_id == sportmonks_fixture_id)
            .order_by(DecisionRecord.id.asc())
        ).all()

    if not existing_rows:
        return {
            "status": "not_ready",
            "version": OUTCOME_SETTLEMENT_VERSION,
            "sportmonks_fixture_id": sportmonks_fixture_id,
            "reason_codes": ["FORWARD_TEST_RECORD_NOT_FOUND"],
        }

    if all(row.settlement_status == "SETTLED" for row in existing_rows):
        return {
            "status": "exists",
            "version": OUTCOME_SETTLEMENT_VERSION,
            "sportmonks_fixture_id": sportmonks_fixture_id,
            "records_settled": 0,
            "records_already_settled": len(existing_rows),
            "records": [_settlement_payload(row) for row in existing_rows],
            "policy": {
                "idempotent": True,
                "settled_records_are_never_overwritten": True,
            },
        }

    # Lazy import avoids a module cycle because SportmonksClient also installs
    # this router during application composition.
    from app.sportmonks import SportmonksClient

    upstream_payload = await SportmonksClient().fixture_result(sportmonks_fixture_id)
    outcome = _parse_fixture_outcome(upstream_payload, sportmonks_fixture_id)
    if outcome.get("status") != "ok":
        return {
            "status": "not_ready",
            "version": OUTCOME_SETTLEMENT_VERSION,
            "sportmonks_fixture_id": sportmonks_fixture_id,
            "reason_codes": list(outcome.get("reason_codes") or []),
            "outcome": outcome,
        }

    actual_result = str(outcome["actual_result"])
    settled_at = datetime.now(timezone.utc)
    settled_count = 0
    already_settled = 0
    unresolved_count = 0
    unresolved: list[dict[str, Any]] = []

    with SessionLocal() as session:
        rows = session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.sportmonks_fixture_id == sportmonks_fixture_id)
            .order_by(DecisionRecord.id.asc())
            .with_for_update()
        ).all()

        for record in rows:
            if record.settlement_status == "SETTLED":
                already_settled += 1
                continue

            won = _selection_won(record.selection, actual_result)
            counterfactual_pnl = _counterfactual_pnl(record.selected_odd, won)
            hypothetical_pnl = _hypothetical_policy_pnl(
                record.decision,
                record.selected_odd,
                won,
            )

            if won is None or counterfactual_pnl is None or hypothetical_pnl is None:
                unresolved_count += 1
                unresolved.append(
                    {
                        "record_id": int(record.id),
                        "reason": "SETTLEMENT_INPUT_INCOMPLETE",
                    }
                )
                continue

            record.actual_result = actual_result
            record.selection_won = "true" if won else "false"
            record.hypothetical_pnl_units = hypothetical_pnl
            record.counterfactual_pnl_units = counterfactual_pnl
            record.settlement_status = "SETTLED"
            record.settled_at = settled_at
            settled_count += 1

        session.commit()

        refreshed = session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.sportmonks_fixture_id == sportmonks_fixture_id)
            .order_by(DecisionRecord.id.asc())
        ).all()

    status = "ok" if unresolved_count == 0 else "partial"
    return {
        "status": status,
        "version": OUTCOME_SETTLEMENT_VERSION,
        "sportmonks_fixture_id": sportmonks_fixture_id,
        "outcome": outcome,
        "records_settled": settled_count,
        "records_already_settled": already_settled,
        "records_unresolved": unresolved_count,
        "unresolved": unresolved,
        "records": [_settlement_payload(row) for row in refreshed],
        "policy": {
            "execution_mode": "RESEARCH_ONLY",
            "idempotent": True,
            "settled_records_are_never_overwritten": True,
            "fulltime_1x2_settles_on_regulation_time": True,
            "extra_time_and_penalties_excluded_from_1x2_result": True,
            "bet_hypothetical_pnl_uses_one_unit": True,
            "no_bet_hypothetical_pnl_is_zero": True,
            "counterfactual_pnl_is_recorded_for_bet_and_no_bet": True,
            "real_money_execution_enabled": False,
        },
    }


async def settle_pending_records(limit: int = 10) -> dict[str, Any]:
    if limit < 1 or limit > MAX_PENDING_FIXTURES_PER_RUN:
        raise ValueError(
            f"limit must be between 1 and {MAX_PENDING_FIXTURES_PER_RUN}"
        )

    ensure_forward_test_schema()
    now = datetime.now(timezone.utc)

    with SessionLocal() as session:
        pending_ids = session.scalars(
            select(DecisionRecord.sportmonks_fixture_id)
            .where(
                DecisionRecord.settlement_status == "UNSETTLED",
                DecisionRecord.fixture_starts_at < now,
            )
            .distinct()
            .order_by(DecisionRecord.sportmonks_fixture_id.asc())
            .limit(limit)
        ).all()

    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    for fixture_id in pending_ids:
        try:
            result = await settle_fixture_records(int(fixture_id))
            items.append(result)
            status = str(result.get("status") or "unknown")
            counts[status] += 1
            counts["records_settled"] += int(result.get("records_settled") or 0)
            counts["records_already_settled"] += int(
                result.get("records_already_settled") or 0
            )
            counts["records_unresolved"] += int(result.get("records_unresolved") or 0)
            for reason in result.get("reason_codes") or []:
                reason_counts[str(reason)] += 1
        except Exception as exc:
            counts["failed"] += 1
            reason_counts["SETTLEMENT_EXCEPTION"] += 1
            items.append(
                {
                    "status": "failed",
                    "version": OUTCOME_SETTLEMENT_VERSION,
                    "sportmonks_fixture_id": int(fixture_id),
                    "error": exc.__class__.__name__,
                }
            )

    return {
        "status": "ok" if counts["failed"] == 0 else "partial",
        "version": OUTCOME_SETTLEMENT_VERSION,
        "evaluated_at": now.isoformat(),
        "selected_pending_fixtures": len(pending_ids),
        "limit": limit,
        "summary": {
            "settled_fixtures": counts["ok"],
            "already_settled_fixtures": counts["exists"],
            "not_ready_fixtures": counts["not_ready"],
            "partial_fixtures": counts["partial"],
            "failed_fixtures": counts["failed"],
            "records_settled": counts["records_settled"],
            "records_already_settled": counts["records_already_settled"],
            "records_unresolved": counts["records_unresolved"],
            "reason_code_counts": dict(sorted(reason_counts.items())),
        },
        "items": items,
        "policy": {
            "execution_mode": "RESEARCH_ONLY",
            "finished_fixture_required": True,
            "max_pending_fixtures_per_run": MAX_PENDING_FIXTURES_PER_RUN,
            "fulltime_1x2_settles_on_regulation_time": True,
            "settled_records_are_never_overwritten": True,
            "real_money_execution_enabled": False,
        },
    }


@router.post("/research/forward-test/settle/fixture/{sportmonks_fixture_id}")
async def settle_fixture_endpoint(sportmonks_fixture_id: int) -> dict[str, Any]:
    try:
        return await settle_fixture_records(sportmonks_fixture_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


@router.post("/research/forward-test/settle/pending")
async def settle_pending_endpoint(
    limit: int = Query(default=10, ge=1, le=MAX_PENDING_FIXTURES_PER_RUN),
) -> dict[str, Any]:
    try:
        return await settle_pending_records(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


def install_outcome_settlement_routes() -> None:
    global _routes_installed
    if _routes_installed:
        return
    forward_test_router.include_router(router)
    _routes_installed = True
