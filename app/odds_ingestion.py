from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Fixture, OddsSnapshot


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _name_or_id(raw: dict, nested_key: str, id_key: str, prefix: str) -> str:
    nested = raw.get(nested_key) or {}
    name = nested.get("name") or nested.get("developer_name")
    if name:
        return str(name)
    identifier = raw.get(id_key)
    return f"{prefix}:{identifier}" if identifier is not None else f"{prefix}:unknown"


def _is_core_market(market_name: str) -> bool:
    name = market_name.lower().strip()

    blocked_tokens = (
        "1st half",
        "first half",
        "2nd half",
        "second half",
        "corner",
        "card",
        "player",
        "goalscorer",
        "booking",
        "throw",
        "foul",
        "tackle",
        "shot",
    )
    if any(token in name for token in blocked_tokens):
        return False

    core_tokens = (
        "fulltime result",
        "full time result",
        "match winner",
        "3-way result",
        "3 way result",
        "both teams to score",
        "btts",
        "over/under",
        "over under",
        "total goals",
        "goal line",
    )
    return any(token in name for token in core_tokens)


def ingest_prematch_odds_payload(
    sportmonks_fixture_id: int,
    payload: dict,
    snapshot_window: str | None = None,
) -> dict:
    rows = payload.get("data") or []

    with SessionLocal() as session:
        fixture = session.scalar(
            select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
        )
        if fixture is None:
            return {
                "status": "fixture_not_found",
                "sportmonks_fixture_id": sportmonks_fixture_id,
                "created": 0,
                "skipped": 0,
                "errors": [],
            }

        created = 0
        skipped = 0
        filtered_out = 0
        errors: list[dict] = []

        for raw in rows:
            try:
                raw_value = raw.get("value")
                label = raw.get("label") or raw.get("name")
                if raw_value is None or not label:
                    skipped += 1
                    continue

                bookmaker = _name_or_id(raw, "bookmaker", "bookmaker_id", "bookmaker")
                market = _name_or_id(raw, "market", "market_id", "market")

                if not _is_core_market(market):
                    filtered_out += 1
                    continue

                odd = Decimal(str(raw_value))
                if odd <= 1:
                    skipped += 1
                    continue

                session.add(
                    OddsSnapshot(
                        fixture_id=fixture.id,
                        bookmaker=bookmaker[:120],
                        market=market[:80],
                        selection=str(label)[:120],
                        odd=odd,
                        source="sportmonks",
                        source_updated_at=_parse_datetime(raw.get("last_update")),
                        snapshot_window=snapshot_window,
                    )
                )
                created += 1
            except (InvalidOperation, ValueError, TypeError) as exc:
                skipped += 1
                errors.append(
                    {
                        "bookmaker_id": raw.get("bookmaker_id"),
                        "market_id": raw.get("market_id"),
                        "label": raw.get("label"),
                        "error": exc.__class__.__name__,
                    }
                )

        session.commit()

    return {
        "status": "ok",
        "sportmonks_fixture_id": sportmonks_fixture_id,
        "received": len(rows),
        "created": created,
        "filtered_out": filtered_out,
        "skipped": skipped,
        "snapshot_window": snapshot_window,
        "errors": errors,
    }
