from __future__ import annotations

from datetime import date, timedelta

from app.ingestion import ingest_fixtures_payload
from app.sportmonks import SportmonksClient

MAX_BACKFILL_DAYS = 31


async def backfill_fixtures(start_date: date, end_date: date) -> dict:
    if end_date < start_date:
        return {"status": "invalid_range", "detail": "end_date must be on or after start_date"}
    day_count = (end_date - start_date).days + 1
    if day_count > MAX_BACKFILL_DAYS:
        return {"status": "range_too_large", "detail": f"Maximum range per request is {MAX_BACKFILL_DAYS} days", "requested_days": day_count}

    client = SportmonksClient()
    current = start_date
    per_day: list[dict] = []
    totals = {"requested_days": day_count, "completed_days": 0, "received": 0, "created": 0, "updated": 0, "skipped": 0, "failed_days": 0}

    while current <= end_date:
        try:
            payload = await client.fixtures_by_date(current)
            result = ingest_fixtures_payload(payload)
            day_result = {"date": current.isoformat(), "status": result.get("status"), "received": result.get("received", len(payload.get("data") or [])), "created": result.get("created", 0), "updated": result.get("updated", 0), "skipped": result.get("skipped", 0), "errors": result.get("errors", [])}
            per_day.append(day_result)
            if result.get("status") == "ok":
                totals["completed_days"] += 1
                totals["received"] += day_result["received"]
                totals["created"] += day_result["created"]
                totals["updated"] += day_result["updated"]
                totals["skipped"] += day_result["skipped"]
            else:
                totals["failed_days"] += 1
        except Exception as exc:
            totals["failed_days"] += 1
            per_day.append({"date": current.isoformat(), "status": "failed", "received": 0, "created": 0, "updated": 0, "skipped": 0, "errors": [{"error": exc.__class__.__name__}]})
        current += timedelta(days=1)

    return {"status": "ok" if totals["failed_days"] == 0 else "partial", "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "max_days_per_request": MAX_BACKFILL_DAYS, "totals": totals, "days": per_day}
