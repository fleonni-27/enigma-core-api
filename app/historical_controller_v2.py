from __future__ import annotations

from datetime import date

from app.historical_controller import run_historical_controller
from app.upstream_exceptions import count_upstream_exceptions


async def run_historical_controller_v2(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    batch_size: int = 25,
    max_batches_per_month: int = 4,
    ingest_fixtures: bool = True,
    skip_existing: bool = True,
    report_limit: int = 200,
) -> dict:
    result = await run_historical_controller(
        start_date=start_date,
        end_date=end_date,
        leagues=leagues,
        batch_size=batch_size,
        max_batches_per_month=max_batches_per_month,
        ingest_fixtures=ingest_fixtures,
        skip_existing=skip_existing,
        report_limit=report_limit,
    )

    all_collection_complete = True
    total_upstream_exceptions = 0

    for month in result.get("months") or []:
        month_start = date.fromisoformat(month["start_date"])
        month_end = date.fromisoformat(month["end_date"])
        checkpoint = month.get("checkpoint") or {}

        upstream_exceptions = count_upstream_exceptions(
            start_date=month_start,
            end_date=month_end,
            leagues=leagues,
        )
        total_upstream_exceptions += upstream_exceptions

        missing = int(checkpoint.get("missing_snapshots", 0) or 0)
        incomplete = int(checkpoint.get("incomplete_snapshots", 0) or 0)
        exhausted = bool(checkpoint.get("batch_exhausted"))
        truncated = bool(checkpoint.get("report_truncated_or_at_limit"))
        fixture_totals = (month.get("fixtures") or {}).get("totals") or {}
        fixture_failed_days = int(fixture_totals.get("failed_days", 0) or 0)
        fixture_collection_complete = (not ingest_fixtures) or fixture_failed_days == 0

        unresolved_incomplete = max(0, incomplete - upstream_exceptions)
        dataset_complete = missing == 0 and incomplete == 0 and not truncated
        collection_complete = (
            fixture_collection_complete
            and exhausted
            and missing == 0
            and unresolved_incomplete == 0
            and not truncated
        )

        checkpoint["fixture_failed_days"] = fixture_failed_days
        checkpoint["fixture_collection_complete"] = fixture_collection_complete
        checkpoint["upstream_exceptions"] = upstream_exceptions
        checkpoint["unresolved_incomplete_snapshots"] = unresolved_incomplete
        checkpoint["dataset_complete"] = dataset_complete
        checkpoint["collection_complete"] = collection_complete
        checkpoint["month_complete"] = collection_complete
        month["checkpoint"] = checkpoint

        if not collection_complete:
            all_collection_complete = False

    result["version"] = "historical_controller_v2"
    result["status"] = "ok" if all_collection_complete else "partial"
    result.setdefault("totals", {})["upstream_exceptions"] = total_upstream_exceptions
    result["policy"] = {
        **(result.get("policy") or {}),
        "dataset_complete_definition": "no missing snapshots, no incomplete snapshots, and audit not truncated",
        "collection_complete_definition": "fixture collection has no failed days, no pending snapshots, and all incomplete snapshots are formally quarantined as upstream unavailable",
        "upstream_exception_training_eligible": False,
        "upstream_exception_profile_remains": "INCOMPLETE",
    }
    return result
