from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import Fixture


def _parse_starting_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _participant_by_location(participants: list[dict], location: str) -> dict | None:
    for participant in participants:
        meta = participant.get("meta") or {}
        if meta.get("location") == location:
            return participant
    return None


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SQLAlchemyError):
        original = getattr(exc, "orig", None)
        if original is not None:
            return str(original)[:800]
    return str(exc)[:800]


def ingest_fixtures_payload(payload: dict) -> dict:
    fixtures = payload.get("data") or []
    created = 0
    updated = 0
    skipped = 0
    errors: list[dict] = []

    with SessionLocal() as session:
        for raw in fixtures:
            try:
                participants = raw.get("participants") or []
                home = _participant_by_location(participants, "home")
                away = _participant_by_location(participants, "away")
                league = raw.get("league") or {}

                if not home or not away or not raw.get("starting_at"):
                    skipped += 1
                    continue

                sportmonks_id = int(raw["id"])
                existing = session.scalar(
                    select(Fixture).where(Fixture.sportmonks_id == sportmonks_id)
                )

                league_id = raw.get("league_id") or league.get("id")
                values = {
                    "league_id": int(league_id) if league_id is not None else None,
                    "league_name": league.get("name"),
                    "home_team": home.get("name") or "Unknown",
                    "away_team": away.get("name") or "Unknown",
                    "starts_at": _parse_starting_at(raw["starting_at"]),
                    "status": raw.get("result_info") or str(raw.get("state_id") or ""),
                }

                if existing is None:
                    session.add(Fixture(sportmonks_id=sportmonks_id, **values))
                    session.flush()
                    created += 1
                else:
                    for field, value in values.items():
                        setattr(existing, field, value)
                    session.flush()
                    updated += 1
            except Exception as exc:
                session.rollback()
                errors.append({
                    "sportmonks_id": raw.get("id"),
                    "error": exc.__class__.__name__,
                    "detail": _safe_error(exc),
                })

        if errors:
            return {
                "status": "failed",
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "errors": errors,
            }

        session.commit()

    return {
        "status": "ok",
        "received": len(fixtures),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": [],
    }
