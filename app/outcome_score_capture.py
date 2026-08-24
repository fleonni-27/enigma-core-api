from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.fixture_results import (
    fixture_results_by_sportmonks_ids,
    persist_fixture_result,
)
from app.forward_test_ledger import (
    DecisionRecord,
    ensure_forward_test_schema,
)
from app import outcome_settlement as settlement_module

_installed = False
_original_settle_fixture_records = settlement_module.settle_fixture_records
MAX_LEGACY_SCORE_BACKFILL = 25


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


async def backfill_missing_settled_fixture_results(
    limit: int = MAX_LEGACY_SCORE_BACKFILL,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_LEGACY_SCORE_BACKFILL:
        raise ValueError(
            f"limit must be between 1 and {MAX_LEGACY_SCORE_BACKFILL}"
        )

    ensure_forward_test_schema()
    with SessionLocal() as session:
        settled_ids = [
            int(value)
            for value in session.scalars(
                select(DecisionRecord.sportmonks_fixture_id)
                .where(DecisionRecord.settlement_status == "SETTLED")
                .distinct()
                .order_by(DecisionRecord.sportmonks_fixture_id.asc())
            ).all()
        ]

    existing = fixture_results_by_sportmonks_ids(settled_ids)
    missing = [value for value in settled_ids if value not in existing][:limit]
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    if not missing:
        return {
            "status": "ok",
            "selected_missing_fixtures": 0,
            "summary": {
                "persisted": 0,
                "existing": 0,
                "not_ready": 0,
                "conflict": 0,
                "failed": 0,
            },
            "items": [],
            "policy": {
                "decision_records_immutable": True,
                "post_match_result_store_only": True,
            },
        }

    from app.sportmonks import SportmonksClient

    client = SportmonksClient()
    for sportmonks_fixture_id in missing:
        try:
            upstream = await client.fixture_result(sportmonks_fixture_id)
            outcome = settlement_module._parse_fixture_outcome(
                upstream,
                sportmonks_fixture_id,
            )
            if outcome.get("status") != "ok":
                counts["not_ready"] += 1
                items.append(
                    {
                        "sportmonks_fixture_id": sportmonks_fixture_id,
                        "status": "not_ready",
                        "reason_codes": list(outcome.get("reason_codes") or []),
                    }
                )
                continue

            stored = _persist_outcome_score(sportmonks_fixture_id, outcome)
            status = str((stored or {}).get("status") or "not_ready")
            counts[status] += 1
            items.append(
                {
                    "sportmonks_fixture_id": sportmonks_fixture_id,
                    "status": status,
                    "fixture_result": stored,
                    "outcome": outcome,
                }
            )
        except Exception as exc:
            counts["failed"] += 1
            items.append(
                {
                    "sportmonks_fixture_id": sportmonks_fixture_id,
                    "status": "failed",
                    "error": exc.__class__.__name__,
                }
            )

    return {
        "status": "ok" if counts["failed"] == 0 else "partial",
        "selected_missing_fixtures": len(missing),
        "summary": {
            "persisted": counts["persisted"],
            "existing": counts["exists"],
            "not_ready": counts["not_ready"],
            "conflict": counts["conflict"],
            "failed": counts["failed"],
        },
        "items": items,
        "policy": {
            "decision_records_immutable": True,
            "post_match_result_store_only": True,
            "conflicting_final_scores_are_never_overwritten": True,
        },
    }


def install_outcome_score_capture() -> None:
    global _installed
    if _installed:
        return
    settlement_module.settle_fixture_records = _settle_fixture_records_with_score
    _installed = True
