from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Numeric, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, SessionLocal
from app.decision_engine import _is_1x2_market, _selection_side
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.league_registry import canonical_league
from app.models import Fixture, OddsSnapshot
from app.odds_ingestion import ingest_prematch_odds_payload
from app.sportmonks import SportmonksClient

ODDS_WINDOW_CLV_VERSION = "odds_window_clv_v1"
CLV_ENGINE_VERSION = "clv_engine_v1"
BUSINESS_TIMEZONE = "America/Sao_Paulo"
CLOSING_TARGET_LEAD_MINUTES = 5
DEFAULT_CLOSING_CONCURRENCY = 4
DEFAULT_CLOSING_MAX_FIXTURES = 20
MAX_QUOTE_SPAN_SECONDS = 300

router = APIRouter(prefix="/operations", tags=["operations"])


class CLVRecord(Base):
    __tablename__ = "clv_records"
    __table_args__ = (
        UniqueConstraint("decision_record_id", name="uq_clv_records_decision_record_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    decision_record_id: Mapped[int] = mapped_column(ForeignKey("decision_records.id"), unique=True, index=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    sportmonks_fixture_id: Mapped[int] = mapped_column(BigInteger, index=True)
    decision: Mapped[str] = mapped_column(String(12), index=True)
    selection: Mapped[str] = mapped_column(String(2))
    bookmaker: Mapped[str] = mapped_column(String(120), index=True)
    market_name: Mapped[str] = mapped_column(String(120))
    decision_snapshot_window: Mapped[str] = mapped_column(String(30), index=True)
    closing_snapshot_window: Mapped[str] = mapped_column(String(30), index=True)
    decision_odd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    closing_odd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    decision_no_vig_probability: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    closing_no_vig_probability: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    clv_odds_decimal: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    clv_odds_pct: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    clv_probability_points: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    clv_probability_pp: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    calibrated_confidence: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    model_edge_vs_closing: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    closing_quote_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_date(value: datetime):
    return _aware_utc(value).astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date()


def daily_window(fixture: Fixture) -> str:
    return f"daily_{_local_date(fixture.starts_at).strftime('%Y%m%d')}"


def j1_window(fixture: Fixture) -> str:
    return f"j1_45m_{_local_date(fixture.starts_at).strftime('%Y%m%d')}"


def closing_window(fixture: Fixture | DecisionRecord) -> str:
    starts_at = fixture.starts_at if isinstance(fixture, Fixture) else fixture.fixture_starts_at
    return f"closing_{_local_date(starts_at).strftime('%Y%m%d')}"


def _row_time(row: OddsSnapshot, *, opening: bool = False) -> datetime:
    value = (row.first_seen_at if opening else row.fetched_at) or row.fetched_at
    return _aware_utc(value)


def _complete_1x2_markets(
    rows: list[OddsSnapshot],
    *,
    home_team: str,
    away_team: str,
    opening: bool = False,
    latest: bool = True,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, OddsSnapshot]] = {}
    for row in rows:
        if not _is_1x2_market(row.market):
            continue
        side = _selection_side(row.selection, home_team, away_team)
        if side is None:
            continue
        key = (row.bookmaker, row.market)
        by_side = groups.setdefault(key, {})
        current = by_side.get(side)
        if current is None:
            by_side[side] = row
            continue
        row_key = (_row_time(row, opening=opening), int(row.id or 0))
        current_key = (_row_time(current, opening=opening), int(current.id or 0))
        if (latest and row_key > current_key) or (not latest and row_key < current_key):
            by_side[side] = row

    result: list[dict[str, Any]] = []
    for (bookmaker, market), by_side in groups.items():
        if any(side not in by_side for side in ("1", "X", "2")):
            continue
        chosen = [by_side["1"], by_side["X"], by_side["2"]]
        times = [_row_time(row, opening=opening) for row in chosen]
        quote_span = (max(times) - min(times)).total_seconds()
        odds = {side: float(by_side[side].odd) for side in ("1", "X", "2")}
        implied = {side: 1.0 / odd for side, odd in odds.items()}
        implied_sum = sum(implied.values())
        no_vig = {side: implied[side] / implied_sum for side in implied}
        result.append(
            {
                "bookmaker": bookmaker,
                "market_name": market,
                "odds": odds,
                "no_vig_probabilities": no_vig,
                "overround": implied_sum - 1.0,
                "quote_span_seconds": round(quote_span, 3),
                "coherent": quote_span <= MAX_QUOTE_SPAN_SECONDS,
                "first_quote_at": min(times).isoformat(),
                "latest_quote_at": max(times).isoformat(),
            }
        )
    result.sort(key=lambda item: (item["bookmaker"], item["market_name"]))
    return result


def _window_payload(
    rows: list[OddsSnapshot],
    *,
    fixture: Fixture,
    source_window: str | None,
    opening: bool = False,
) -> dict[str, Any]:
    markets = _complete_1x2_markets(
        rows,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        opening=opening,
        latest=not opening,
    )
    observed = [
        _row_time(row, opening=opening)
        for row in rows
        if row.fetched_at is not None
    ]
    return {
        "source_window": source_window,
        "market_count": len(markets),
        "observed_from": min(observed).isoformat() if observed else None,
        "observed_to": max(observed).isoformat() if observed else None,
        "markets": markets,
    }


def build_fixture_odds_windows(sportmonks_fixture_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        fixture = session.scalar(select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id))
        if fixture is None:
            return {"status": "fixture_not_found", "version": ODDS_WINDOW_CLV_VERSION}
        kickoff = _aware_utc(fixture.starts_at)
        rows = session.scalars(
            select(OddsSnapshot)
            .where(OddsSnapshot.fixture_id == fixture.id, OddsSnapshot.fetched_at < kickoff)
            .order_by(OddsSnapshot.fetched_at.asc(), OddsSnapshot.id.asc())
        ).all()
        session.expunge(fixture)
        for row in rows:
            session.expunge(row)

    j0_name = daily_window(fixture)
    j1_name = j1_window(fixture)
    closing_name = closing_window(fixture)
    return {
        "status": "ok",
        "version": ODDS_WINDOW_CLV_VERSION,
        "fixture": {
            "fixture_id": int(fixture.id),
            "sportmonks_fixture_id": int(fixture.sportmonks_id),
            "league": fixture.league_name,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "starts_at": kickoff.isoformat(),
        },
        "windows": {
            "opening": _window_payload(rows, fixture=fixture, source_window=None, opening=True),
            "j0": _window_payload([r for r in rows if r.snapshot_window == j0_name], fixture=fixture, source_window=j0_name),
            "j1": _window_payload([r for r in rows if r.snapshot_window == j1_name], fixture=fixture, source_window=j1_name),
            "closing": _window_payload([r for r in rows if r.snapshot_window == closing_name], fixture=fixture, source_window=closing_name),
        },
        "policy": {
            "opening_definition": "earliest_observed_complete_1x2_proxy_not_exchange_true_open",
            "j0_definition": "same_match_day_daily_odds_stream",
            "j1_definition": "existing_j1_45m_stream",
            "closing_definition": "latest_observed_complete_1x2_quote_before_kickoff_from_closing_5m_stream",
            "bookmaker_margin_removed_with_no_vig": True,
        },
    }


def _closing_due_target_fixtures(*, now: datetime, max_fixtures: int) -> list[Fixture]:
    upper = now + timedelta(minutes=CLOSING_TARGET_LEAD_MINUTES)
    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at > now, Fixture.starts_at <= upper)
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()
        for fixture in candidates:
            session.expunge(fixture)

    target: list[Fixture] = []
    for fixture in candidates:
        canonical = canonical_league(fixture.league_name)
        if canonical.get("target") and canonical.get("key"):
            target.append(fixture)
        if len(target) >= max_fixtures:
            break
    return target


async def _capture_closing_odds(fixtures: list[Fixture]) -> dict[str, Any]:
    if not fixtures:
        return {"status": "ok", "fixtures": 0, "items": [], "transport": {}}
    semaphore = asyncio.Semaphore(DEFAULT_CLOSING_CONCURRENCY)
    items: list[dict[str, Any]] = []

    async with SportmonksClient() as client:
        async def fetch(fixture: Fixture) -> tuple[Fixture, dict[str, Any] | Exception]:
            async with semaphore:
                try:
                    return fixture, await client.prematch_odds_by_fixture(int(fixture.sportmonks_id))
                except Exception as exc:
                    return fixture, exc

        fetched = await asyncio.gather(*(fetch(fixture) for fixture in fixtures))
        for fixture, payload in fetched:
            if isinstance(payload, Exception):
                items.append({
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "status": "upstream_failed",
                    "error": payload.__class__.__name__,
                })
                continue
            try:
                result = ingest_prematch_odds_payload(
                    sportmonks_fixture_id=int(fixture.sportmonks_id),
                    payload=payload,
                    snapshot_window=closing_window(fixture),
                )
                items.append({
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "status": result.get("status"),
                    "created": int(result.get("created") or 0),
                    "movements_created": int(result.get("movements_created") or 0),
                    "deduplicated_unchanged": int(result.get("deduplicated_unchanged") or 0),
                    "snapshot_window": closing_window(fixture),
                })
            except Exception as exc:
                items.append({
                    "sportmonks_fixture_id": int(fixture.sportmonks_id),
                    "status": "ingestion_failed",
                    "error": exc.__class__.__name__,
                })
        transport = client.transport_audit()

    failed = sum(1 for item in items if item.get("status") not in {"ok"})
    return {
        "status": "partial" if failed else "ok",
        "fixtures": len(fixtures),
        "failed": failed,
        "items": items,
        "transport": transport,
    }


def _matching_closing_market(session, record: DecisionRecord) -> dict[str, Any] | None:
    rows = session.scalars(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == record.fixture_id,
            OddsSnapshot.snapshot_window == closing_window(record),
            OddsSnapshot.bookmaker == record.bookmaker,
            OddsSnapshot.market == record.market_name,
            OddsSnapshot.fetched_at < record.fixture_starts_at,
        )
        .order_by(OddsSnapshot.fetched_at.desc(), OddsSnapshot.id.desc())
    ).all()
    markets = _complete_1x2_markets(
        rows,
        home_team=record.home_team,
        away_team=record.away_team,
        latest=True,
    )
    coherent = [market for market in markets if market["coherent"]]
    return coherent[0] if coherent else None


def _clv_payload(record: DecisionRecord, market: dict[str, Any]) -> dict[str, Any] | None:
    selection = str(record.selection or "")
    if selection not in {"1", "X", "2"} or record.selected_odd is None:
        return None
    closing_odd = float((market.get("odds") or {}).get(selection) or 0)
    closing_no_vig = float((market.get("no_vig_probabilities") or {}).get(selection) or 0)
    if closing_odd <= 1 or closing_no_vig <= 0:
        return None
    decision_odd = float(record.selected_odd)
    decision_no_vig = float(record.selected_no_vig_probability) if record.selected_no_vig_probability is not None else None
    calibrated = float(record.calibrated_favorite_confidence) if record.calibrated_favorite_confidence is not None else None
    clv_odds = decision_odd / closing_odd - 1.0
    clv_probability = closing_no_vig - decision_no_vig if decision_no_vig is not None else None
    model_edge_closing = calibrated - closing_no_vig if calibrated is not None else None
    return {
        "decision_record_id": int(record.id),
        "fixture_id": int(record.fixture_id),
        "sportmonks_fixture_id": int(record.sportmonks_fixture_id),
        "decision": record.decision,
        "selection": selection,
        "bookmaker": record.bookmaker,
        "market_name": record.market_name,
        "decision_snapshot_window": record.snapshot_window,
        "closing_snapshot_window": closing_window(record),
        "decision_odd": decision_odd,
        "closing_odd": closing_odd,
        "decision_no_vig_probability": decision_no_vig,
        "closing_no_vig_probability": closing_no_vig,
        "clv_odds_decimal": clv_odds,
        "clv_odds_pct": clv_odds * 100.0,
        "clv_probability_points": clv_probability,
        "clv_probability_pp": clv_probability * 100.0 if clv_probability is not None else None,
        "calibrated_confidence": calibrated,
        "model_edge_vs_closing": model_edge_closing,
        "closing_quote_fetched_at": market["latest_quote_at"],
        "positive_clv": clv_odds > 0,
    }


def finalize_pending_clv(*, now: datetime | None = None, limit: int = 100) -> dict[str, Any]:
    effective_now = _aware_utc(now or datetime.now(timezone.utc))
    ensure_forward_test_schema()
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    with SessionLocal() as session:
        existing_ids = select(CLVRecord.decision_record_id)
        records = session.scalars(
            select(DecisionRecord)
            .where(
                DecisionRecord.fixture_starts_at <= effective_now,
                DecisionRecord.selected_odd.is_not(None),
                DecisionRecord.selection.is_not(None),
                DecisionRecord.bookmaker.is_not(None),
                DecisionRecord.market_name.is_not(None),
                DecisionRecord.id.not_in(existing_ids),
            )
            .order_by(DecisionRecord.fixture_starts_at.asc(), DecisionRecord.id.asc())
            .limit(limit)
        ).all()

        for record in records:
            market = _matching_closing_market(session, record)
            if market is None:
                counts["closing_not_ready"] += 1
                continue
            payload = _clv_payload(record, market)
            if payload is None:
                counts["not_computable"] += 1
                continue
            row = CLVRecord(
                decision_record_id=record.id,
                fixture_id=record.fixture_id,
                sportmonks_fixture_id=record.sportmonks_fixture_id,
                decision=record.decision,
                selection=payload["selection"],
                bookmaker=str(record.bookmaker),
                market_name=str(record.market_name),
                decision_snapshot_window=record.snapshot_window,
                closing_snapshot_window=payload["closing_snapshot_window"],
                decision_odd=Decimal(str(payload["decision_odd"])),
                closing_odd=Decimal(str(payload["closing_odd"])),
                decision_no_vig_probability=(Decimal(str(payload["decision_no_vig_probability"])) if payload["decision_no_vig_probability"] is not None else None),
                closing_no_vig_probability=Decimal(str(payload["closing_no_vig_probability"])),
                clv_odds_decimal=Decimal(str(payload["clv_odds_decimal"])),
                clv_odds_pct=Decimal(str(payload["clv_odds_pct"])),
                clv_probability_points=(Decimal(str(payload["clv_probability_points"])) if payload["clv_probability_points"] is not None else None),
                clv_probability_pp=(Decimal(str(payload["clv_probability_pp"])) if payload["clv_probability_pp"] is not None else None),
                calibrated_confidence=(Decimal(str(payload["calibrated_confidence"])) if payload["calibrated_confidence"] is not None else None),
                model_edge_vs_closing=(Decimal(str(payload["model_edge_vs_closing"])) if payload["model_edge_vs_closing"] is not None else None),
                closing_quote_fetched_at=datetime.fromisoformat(payload["closing_quote_fetched_at"].replace("Z", "+00:00")),
                payload=payload,
            )
            session.add(row)
            try:
                session.commit()
                counts["finalized"] += 1
                items.append(payload)
            except IntegrityError:
                session.rollback()
                counts["already_finalized_race"] += 1

    return {
        "status": "ok",
        "version": CLV_ENGINE_VERSION,
        "evaluated_at": effective_now.isoformat(),
        "selected_records": len(records),
        "counts": dict(counts),
        "items": items,
        "policy": {
            "decision_record_mutated": False,
            "clv_record_immutable": True,
            "same_bookmaker_same_market_required": True,
            "complete_coherent_1x2_closing_required": True,
            "positive_odds_clv_formula": "decision_odd / closing_odd - 1",
            "positive_probability_clv_formula": "closing_no_vig_probability - decision_no_vig_probability",
        },
    }


async def run_odds_window_clv_cycle(*, max_fixtures: int = DEFAULT_CLOSING_MAX_FIXTURES) -> dict[str, Any]:
    if max_fixtures < 1 or max_fixtures > 100:
        raise ValueError("max_fixtures must be between 1 and 100")
    now = datetime.now(timezone.utc)
    fixtures = _closing_due_target_fixtures(now=now, max_fixtures=max_fixtures)
    capture = await _capture_closing_odds(fixtures)
    clv = finalize_pending_clv(now=now, limit=100)
    return {
        "status": "partial" if capture.get("status") == "partial" else "ok",
        "version": ODDS_WINDOW_CLV_VERSION,
        "evaluated_at": now.isoformat(),
        "closing": {
            "target_lead_minutes": CLOSING_TARGET_LEAD_MINUTES,
            "selected_fixtures": len(fixtures),
            "capture": capture,
        },
        "clv": clv,
    }


def build_clv_report(*, limit: int = 200, decision: str | None = None) -> dict[str, Any]:
    with SessionLocal() as session:
        query = select(CLVRecord).order_by(CLVRecord.finalized_at.desc(), CLVRecord.id.desc())
        if decision:
            query = query.where(CLVRecord.decision == decision)
        rows = session.scalars(query.limit(limit)).all()

    odds_values = [float(row.clv_odds_decimal) for row in rows]
    prob_values = [float(row.clv_probability_points) for row in rows if row.clv_probability_points is not None]
    positive = sum(1 for value in odds_values if value > 0)
    return {
        "status": "ok",
        "version": CLV_ENGINE_VERSION,
        "sample_size": len(rows),
        "summary": {
            "positive_clv_count": positive,
            "positive_clv_rate": (positive / len(rows)) if rows else None,
            "average_clv_odds_pct": (sum(odds_values) / len(odds_values) * 100.0) if odds_values else None,
            "average_clv_probability_pp": (sum(prob_values) / len(prob_values) * 100.0) if prob_values else None,
        },
        "items": [dict(row.payload or {}) for row in rows],
    }


@router.get("/odds-window/fixture/{sportmonks_fixture_id}")
def odds_window_fixture_endpoint(sportmonks_fixture_id: int) -> dict[str, Any]:
    result = build_fixture_odds_windows(sportmonks_fixture_id)
    if result.get("status") == "fixture_not_found":
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/clv")
def clv_report_endpoint(
    limit: int = Query(default=200, ge=1, le=500),
    decision: str | None = Query(default=None),
) -> dict[str, Any]:
    if decision is not None and decision not in {"BET", "NO_BET"}:
        raise HTTPException(status_code=400, detail="decision must be BET or NO_BET")
    return build_clv_report(limit=limit, decision=decision)
