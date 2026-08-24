from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.fixture_results import (
    fixture_results_by_sportmonks_ids,
    persist_fixture_result,
)
from app.forward_test_ledger import DecisionRecord
from app import outcome_settlement as settlement_module

_installed = False
_original_settle_fixture_records = settlement_module.settle_fixture_records


def _fixture_id_for_sportmonks_id(sportmonks_fixture_id: int) -> int | None:
    with SessionLocal() as session:
        value = session.scalar(
            select(DecisionRecord.fixture_id)
            .where(DecisionRecord.sportmonks_fixture_id == sportmonks_fixture_id)
            .order_by(DecisionRecord.id.asc())
            .limit(1)
        )
    return int(value) if value is not None else None


def _persist_outcome_score(
    sportmonks_fixture_id: int,
    outcome: dict[str, Any],
) -> dict[str, Any] | None:
    if outcome.get("status") != "ok":
        return None
    fixture_id = _fixture_id_for_sportmonks_id(sportmonks_fixture_id)
    if fixture_id is None:
        return None

    score = outcome.get("regulation_score") or {}
    home_goals = score.get("home")
    away_goals = score.get("away")
    if home_goals is None or away_goals is None:
        return None

    state = outcome.get("state") or {}
    return persist_fixture_result(
        fixture_id=fixture_id,
        sportmonks_fixture_id=sportmonks_fixture_id,
        home_goals=int(home_goals),
        away_goals=int(away_goals),
        actual_result=str(outcome.get("actual_result") or ""),
        score_source=str(score.get("source") or "SPORTMONKS"),
        state_id=state.get("id"),
        state_code=state.get("code"),
    )


async def _settle_fixture_records_with_score(
    sportmonks_fixture_id: int,
) -> dict[str, Any]:
    result = await _original_settle_fixture_records(sportmonks_fixture_id)

    outcome = result.get("outcome") or {}
    stored = _persist_outcome_score(sportmonks_fixture_id, outcome)

    if stored is None and result.get("status") == "exists":
        existing = fixture_results_by_sportmonks_ids([sportmonks_fixture_id]).get(
            int(sportmonks_fixture_id)
        )
        if existing is not None:
            stored = {"status": "exists", "record": existing}
        else:
            # The decision can predate fixture_result_v1. In that case, enrich
            # only the separate post-match result store; never rewrite the
            # already-settled DecisionRecord.
            from app.sportmonks import SportmonksClient

            upstream = await SportmonksClient().fixture_result(sportmonks_fixture_id)
            historical_outcome = settlement_module._parse_fixture_outcome(
                upstream,
                sportmonks_fixture_id,
            )
            stored = _persist_outcome_score(
                sportmonks_fixture_id,
                historical_outcome,
            )
            if historical_outcome.get("status") == "ok":
                result["outcome"] = historical_outcome

    if stored is not None:
        result["fixture_result"] = stored
    result.setdefault("policy", {})["final_score_stored_separately_from_decision"] = True
    result["policy"]["settled_decision_fields_never_rewritten_for_score_capture"] = True
    return result


def install_outcome_score_capture() -> None:
    global _installed
    if _installed:
        return
    settlement_module.settle_fixture_records = _settle_fixture_records_with_score
    _installed = True
