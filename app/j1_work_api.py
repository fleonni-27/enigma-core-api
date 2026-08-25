from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.database import SessionLocal
from app.j1_work_queue import (
    J1WorkItem,
    ensure_j1_work_queue_schema,
    queue_status,
    work_item_dict,
)

router = APIRouter(prefix="/operations/j1-work", tags=["operations"])


@router.get("/status")
def j1_work_status() -> dict:
    return queue_status()


@router.get("/recent")
def j1_work_recent(
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    ensure_j1_work_queue_schema()
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(J1WorkItem)
                .order_by(J1WorkItem.id.desc())
                .limit(limit)
            ).all()
        )
    return {
        "status": "ok",
        "limit": limit,
        "items": [work_item_dict(row) for row in rows],
        "claim_tokens_exposed": False,
    }
