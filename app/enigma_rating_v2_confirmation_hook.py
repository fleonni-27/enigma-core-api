from __future__ import annotations

import logging
from typing import Any

import app.daily_prediction_runner_v2 as runner
from app.enigma_rating_v2_confirmation_holdout import capture_confirmation_holdout_target

logger = logging.getLogger(__name__)
_installed = False
_original_persist_evaluated_decision = runner.persist_evaluated_decision


def install_confirmation_holdout_j1_hook() -> None:
    global _installed
    if _installed:
        return

    def persist_with_confirmation_capture(
        decision_result: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        ledger = _original_persist_evaluated_decision(decision_result, source=source)
        if ledger.get("status") not in {"persisted", "exists"}:
            return ledger

        record = ledger.get("record") or {}
        record_id = record.get("record_id")
        if record_id is None:
            return ledger

        try:
            holdout = capture_confirmation_holdout_target(int(record_id))
        except Exception as exc:
            logger.exception(
                "confirmation holdout capture failed ledger_record_id=%s",
                record_id,
            )
            holdout = {
                "status": "capture_failed",
                "reason_codes": [exc.__class__.__name__],
            }

        # This audit field is not persisted into DecisionRecord and does not alter
        # any Decision Engine semantics. The dedicated holdout registry is the
        # immutable source of truth for confirmation membership.
        ledger["confirmation_holdout"] = holdout
        return ledger

    runner.persist_evaluated_decision = persist_with_confirmation_capture
    _installed = True
