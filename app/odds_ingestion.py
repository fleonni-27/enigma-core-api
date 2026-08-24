from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.models import Fixture, OddsSnapshot

ODDS_QUOTE_DEDUPE_VERSION = "odds_quote_dedupe_v1"
ODD_STORAGE_QUANTUM = Decimal("0.0001")
ODDS_SOURCE = "sportmonks"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _stored_odd(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(ODD_STORAGE_QUANTUM, rounding=ROUND_HALF_UP)


def _quote_key(
    bookmaker: str,
    market: str,
    selection: str,
    source: str = ODDS_SOURCE,
) -> tuple[str, str, str, str]:
    return bookmaker, market, selection, source


def _dedupe_lock_key(fixture_id: int, snapshot_window: str | None) -> int:
    raw = f"odds-dedupe|{fixture_id}|{snapshot_window or '<none>'}".encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _lock_fixture_window(session, fixture_id: int, snapshot_window: str | None) -> None:
    """Serialize same fixture/window ingestion across PostgreSQL workers."""

    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _dedupe_lock_key(fixture_id, snapshot_window)},
    )


def _latest_stream_state(
    session,
    *,
    fixture_id: int,
    snapshot_window: str | None,
) -> dict[tuple[str, str, str, str], OddsSnapshot]:
    window_condition = (
        OddsSnapshot.snapshot_window.is_(None)
        if snapshot_window is None
        else OddsSnapshot.snapshot_window == snapshot_window
    )
    ranked = (
        select(
            OddsSnapshot.id.label("odds_id"),
            func.row_number()
            .over(
                partition_by=(
                    OddsSnapshot.bookmaker,
                    OddsSnapshot.market,
                    OddsSnapshot.selection,
                    OddsSnapshot.source,
                ),
                order_by=(OddsSnapshot.fetched_at.desc(), OddsSnapshot.id.desc()),
            )
            .label("row_number"),
        )
        .where(OddsSnapshot.fixture_id == fixture_id, window_condition)
        .subquery()
    )
    rows = session.scalars(
        select(OddsSnapshot)
        .join(ranked, OddsSnapshot.id == ranked.c.odds_id)
        .where(ranked.c.row_number == 1)
    ).all()
    return {
        _quote_key(row.bookmaker, row.market, row.selection, row.source): row
        for row in rows
    }


def _newer_datetime(current: datetime | None, incoming: datetime | None) -> datetime | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(current.astimezone(timezone.utc), incoming.astimezone(timezone.utc))


def ingest_prematch_odds_payload(
    sportmonks_fixture_id: int,
    payload: dict,
    snapshot_window: str | None = None,
) -> dict:
    """Persist only real price-state changes while keeping quote freshness.

    A logical stream is fixture + snapshot window + bookmaker + market + selection
    + source. The first observed price is inserted. Re-observing the same price
    updates the existing state's `fetched_at` (latest observation) and
    `observation_count`, while `first_seen_at` remains unchanged. A different
    price inserts a new row, including a return to a previously seen price
    (1.85 -> 1.87 -> 1.85), so genuine movement history is never collapsed.
    """

    rows = payload.get("data") or []
    observed_at = datetime.now(timezone.utc)

    with SessionLocal() as session:
        fixture = session.scalar(
            select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id)
        )
        if fixture is None:
            return {
                "status": "fixture_not_found",
                "version": ODDS_QUOTE_DEDUPE_VERSION,
                "sportmonks_fixture_id": sportmonks_fixture_id,
                "created": 0,
                "deduplicated_unchanged": 0,
                "skipped": 0,
                "errors": [],
            }

        _lock_fixture_window(session, int(fixture.id), snapshot_window)
        latest_by_stream = _latest_stream_state(
            session,
            fixture_id=int(fixture.id),
            snapshot_window=snapshot_window,
        )

        created = skipped = filtered_out = 0
        initial_states_created = movements_created = deduplicated_unchanged = 0
        errors: list[dict] = []

        for raw in rows:
            try:
                raw_value = raw.get("value")
                label = raw.get("label") or raw.get("name")
                if raw_value is None or not label:
                    skipped += 1
                    continue

                bookmaker = _name_or_id(raw, "bookmaker", "bookmaker_id", "bookmaker")[:120]
                market = _name_or_id(raw, "market", "market_id", "market")[:80]
                if not _is_core_market(market):
                    filtered_out += 1
                    continue

                odd = _stored_odd(raw_value)
                if odd <= 1:
                    skipped += 1
                    continue

                selection = str(label)[:120]
                source_updated_at = _parse_datetime(raw.get("last_update"))
                key = _quote_key(bookmaker, market, selection)
                previous = latest_by_stream.get(key)

                if previous is not None and _stored_odd(previous.odd) == odd:
                    if previous.first_seen_at is None:
                        previous.first_seen_at = previous.fetched_at
                    previous.fetched_at = _newer_datetime(
                        previous.fetched_at,
                        observed_at,
                    )
                    previous.observation_count = int(previous.observation_count or 1) + 1
                    previous.source_updated_at = _newer_datetime(
                        previous.source_updated_at,
                        source_updated_at,
                    )
                    deduplicated_unchanged += 1
                    continue

                state = OddsSnapshot(
                    fixture_id=fixture.id,
                    bookmaker=bookmaker,
                    market=market,
                    selection=selection,
                    odd=odd,
                    source=ODDS_SOURCE,
                    source_updated_at=source_updated_at,
                    first_seen_at=observed_at,
                    fetched_at=observed_at,
                    observation_count=1,
                    snapshot_window=snapshot_window,
                )
                session.add(state)
                latest_by_stream[key] = state
                created += 1
                if previous is None:
                    initial_states_created += 1
                else:
                    movements_created += 1
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
        "version": ODDS_QUOTE_DEDUPE_VERSION,
        "sportmonks_fixture_id": sportmonks_fixture_id,
        "received": len(rows),
        "created": created,
        "initial_states_created": initial_states_created,
        "movements_created": movements_created,
        "deduplicated_unchanged": deduplicated_unchanged,
        "storage_rows_avoided": deduplicated_unchanged,
        "filtered_out": filtered_out,
        "skipped": skipped,
        "snapshot_window": snapshot_window,
        "observed_at": observed_at.isoformat(),
        "errors": errors,
        "policy": {
            "logical_stream": "fixture_window_bookmaker_market_selection_source",
            "same_price_reobservation_inserts_new_row": False,
            "same_price_reobservation_refreshes_fetched_at": True,
            "first_seen_at_is_preserved": True,
            "price_change_inserts_new_row": True,
            "price_return_after_intermediate_change_inserts_new_row": True,
            "postgresql_same_fixture_window_ingestion_serialized": True,
        },
    }
