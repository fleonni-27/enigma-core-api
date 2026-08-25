from __future__ import annotations

import asyncio
import json

from app.daily_prediction_runner_v2 import DEFAULT_MAX_LATENESS_MINUTES
from app.j1_capacity import configured_j1_max_fixtures
from app.j1_pending_selector_v2 import install_j1_pending_selector_v2
from app.j1_scheduler import run_j1_cycle
from app.odds_window_clv import run_odds_window_clv_cycle


async def run_primary_operations_cycle() -> dict:
    install_j1_pending_selector_v2()
    j1 = await run_j1_cycle(
        source="render_cron",
        max_lateness_minutes=DEFAULT_MAX_LATENESS_MINUTES,
        max_fixtures=configured_j1_max_fixtures(),
    )

    # Closing/CLV is deliberately isolated from J1 correctness. A temporary
    # upstream or schema-release problem is visible in the cron payload but
    # cannot make a valid J1 Prediction/Decision/Ledger cycle fail.
    try:
        odds_window_clv = await run_odds_window_clv_cycle()
    except Exception as exc:
        odds_window_clv = {
            "status": "failed",
            "version": "odds_window_clv_v1",
            "error": exc.__class__.__name__,
        }

    j1["odds_window_clv"] = odds_window_clv
    return j1


def main() -> None:
    result = asyncio.run(run_primary_operations_cycle())
    print(json.dumps(result, ensure_ascii=False, default=str))
    health = str((result.get("run_health") or {}).get("status") or "UNKNOWN")
    if result.get("status") != "ok" or health == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()