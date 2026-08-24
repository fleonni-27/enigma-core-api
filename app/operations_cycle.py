from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from app.future_batch import (
    DEFAULT_DAYS_AHEAD,
    DEFAULT_MAX_FIXTURES,
    DEFAULT_MIN_LEAD_MINUTES,
    run_future_batch,
)
from app.outcome_settlement import settle_pending_records

OPERATIONS_CYCLE_VERSION = "operations_cycle_v1"
DEFAULT_SETTLEMENT_LIMIT = 10


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


async def run_operations_cycle(
    *,
    settlement_limit: int = DEFAULT_SETTLEMENT_LIMIT,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    max_fixtures: int = DEFAULT_MAX_FIXTURES,
    min_lead_minutes: int = DEFAULT_MIN_LEAD_MINUTES,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    settlement_result: dict[str, Any] | None = None
    batch_result: dict[str, Any] | None = None
    errors: list[dict[str, str]] = []

    try:
        settlement_result = await settle_pending_records(limit=settlement_limit)
    except Exception as exc:
        errors.append(
            {
                "stage": "settlement",
                "error": exc.__class__.__name__,
            }
        )

    try:
        batch_result = await run_future_batch(
            days_ahead=days_ahead,
            max_fixtures=max_fixtures,
            min_lead_minutes=min_lead_minutes,
            skip_existing_fixtures=True,
        )
    except Exception as exc:
        errors.append(
            {
                "stage": "future_batch",
                "error": exc.__class__.__name__,
            }
        )

    batch_summary = (batch_result or {}).get("summary") or {}
    batch_operational_failures = sum(
        int(batch_summary.get(key) or 0)
        for key in (
            "failed",
            "odds_failed",
            "ledger_failed",
            "ledger_not_persisted",
        )
    )
    settlement_status = str((settlement_result or {}).get("status") or "failed")
    batch_status = str((batch_result or {}).get("status") or "failed")

    if errors or settlement_status == "partial" or batch_operational_failures > 0:
        status = "partial"
    elif settlement_status in {"ok", "exists"} and batch_status == "ok":
        status = "ok"
    else:
        status = "partial"

    finished_at = datetime.now(timezone.utc)
    return {
        "status": status,
        "version": OPERATIONS_CYCLE_VERSION,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "settlement": settlement_result,
        "future_batch": batch_result,
        "errors": errors,
        "policy": {
            "execution_mode": "RESEARCH_ONLY",
            "settlement_runs_before_future_batch": True,
            "future_batch_skip_existing_fixtures": True,
            "http_endpoint_required": False,
            "stake_sizing_enabled": False,
            "real_money_execution_enabled": False,
        },
    }


async def _main_async() -> int:
    try:
        result = await run_operations_cycle(
            settlement_limit=_env_int(
                "ENIGMA_CYCLE_SETTLEMENT_LIMIT",
                DEFAULT_SETTLEMENT_LIMIT,
                1,
                25,
            ),
            days_ahead=_env_int(
                "ENIGMA_CYCLE_DAYS_AHEAD",
                DEFAULT_DAYS_AHEAD,
                0,
                7,
            ),
            max_fixtures=_env_int(
                "ENIGMA_CYCLE_MAX_FIXTURES",
                DEFAULT_MAX_FIXTURES,
                1,
                5,
            ),
            min_lead_minutes=_env_int(
                "ENIGMA_CYCLE_MIN_LEAD_MINUTES",
                DEFAULT_MIN_LEAD_MINUTES,
                0,
                1440,
            ),
        )
    except Exception as exc:
        result = {
            "status": "failed",
            "version": OPERATIONS_CYCLE_VERSION,
            "error": exc.__class__.__name__,
            "policy": {
                "execution_mode": "RESEARCH_ONLY",
                "real_money_execution_enabled": False,
            },
        }

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") == "ok" else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
