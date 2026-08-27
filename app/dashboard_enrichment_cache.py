from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import DashboardEnrichmentSnapshot

CACHE_VERSION = "dashboard_enrichment_cache_v1"


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


def persist_dashboard_enrichment(payloads: dict[int, dict[str, Any]]) -> dict[str, int]:
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
                        payload=payload,
                        generated_at=now,
                    )
                )
                created += 1
            else:
                row.version = CACHE_VERSION
                row.payload = payload
                row.generated_at = now
                updated += 1
        session.commit()
    return {"created": created, "updated": updated}
