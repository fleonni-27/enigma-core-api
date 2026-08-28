from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import DashboardEnrichmentSnapshot

CACHE_VERSION = "dashboard_enrichment_cache_v2_merge"


def load_dashboard_enrichment(fixture_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Read one small materialized row per fixture. No provider/history calls."""
    if not fixture_ids:
        return {}
    with SessionLocal() as session:
        rows = session.scalars(
            select(DashboardEnrichmentSnapshot).where(
                DashboardEnrichmentSnapshot.fixture_id.in_(fixture_ids)
            )
        ).all()
    return {
        int(row.fixture_id): {
            **dict(row.payload or {}),
            "cache_version": row.version,
            "cache_generated_at": row.generated_at.isoformat() if row.generated_at else None,
        }
        for row in rows
    }


def _merged_payload(existing: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, Any]:
    """Top-level merge so independent background/J1 writers do not erase each other."""
    result = dict(existing or {})
    for key, value in patch.items():
        if key == "data_quality" and isinstance(value, dict):
            result[key] = {**dict(result.get(key) or {}), **value}
        else:
            result[key] = value
    return result


def persist_dashboard_enrichment(
    payloads: dict[int, dict[str, Any]],
    *,
    merge: bool = True,
) -> dict[str, int]:
    if not payloads:
        return {"created": 0, "updated": 0}
    now = datetime.now(timezone.utc)
    created = 0
    updated = 0
    with SessionLocal() as session:
        existing = {
            int(row.fixture_id): row
            for row in session.scalars(
                select(DashboardEnrichmentSnapshot).where(
                    DashboardEnrichmentSnapshot.fixture_id.in_(list(payloads))
                )
            ).all()
        }
        for fixture_id, payload in payloads.items():
            row = existing.get(int(fixture_id))
            if row is None:
                session.add(
                    DashboardEnrichmentSnapshot(
                        fixture_id=int(fixture_id),
                        version=CACHE_VERSION,
                        payload=dict(payload),
                        generated_at=now,
                    )
                )
                created += 1
            else:
                row.version = CACHE_VERSION
                row.payload = _merged_payload(dict(row.payload or {}), payload) if merge else dict(payload)
                row.generated_at = now
                updated += 1
        session.commit()
    return {"created": created, "updated": updated}


def merge_dashboard_enrichment(fixture_id: int, patch: dict[str, Any]) -> dict[str, int]:
    return persist_dashboard_enrichment({int(fixture_id): patch}, merge=True)
