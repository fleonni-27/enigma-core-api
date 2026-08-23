from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.database import SessionLocal
from app.decision_engine import (
    DEFAULT_MAX_OVERROUND,
    DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_EXPECTED_VALUE,
    evaluate_fixture_decision,
)
from app.ingestion import ingest_fixtures_payload
from app.league_registry import canonical_league
from app.models import Fixture
from app.odds_ingestion import ingest_prematch_odds_payload
from app.prematch_inference import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_LOOKBACK_MATCHES,
    DEFAULT_MAX_TRAINING_ROWS,
    DEFAULT_MIN_HISTORY_MATCHES,
    DEFAULT_MIN_TRAINING_ROWS,
    DEFAULT_PREDICTION_WINDOW,
    MODEL_VERSION,
    generate_and_persist_prematch_prediction,
)
from app.sportmonks import SportmonksClient

FUTURE_BATCH_VERSION = "future_batch_runner_v1"
DEFAULT_DAYS_AHEAD = 3
DEFAULT_MAX_FIXTURES = 3
MAX_DAYS_AHEAD = 7
MAX_FIXTURES = 5
DEFAULT_MIN_LEAD_MINUTES = 60

router = APIRouter()


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _fixture_payload(fixture: Fixture) -> dict[str, Any]:
    canonical = canonical_league(fixture.league_name)
    return {
        "fixture_id": int(fixture.id),
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "league": canonical.get("canonical_name") or fixture.league_name,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
        "status": fixture.status,
    }


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    if not leagues:
        return set()
    keys: set[str] = set()
    unsupported: list[str] = []
    for league in leagues:
        canonical = canonical_league(league)
        key = canonical.get("key")
        if not canonical.get("target") or not key:
            unsupported.append(str(league))
            continue
        keys.add(str(key))
    if unsupported:
        raise ValueError(f"unsupported target league filter(s): {', '.join(unsupported)}")
    return keys


def _compact_decision(result: dict[str, Any]) -> dict[str, Any]:
    best_market = result.get("best_market") or {}
    market = best_market.get("market") or {}
    value = best_market.get("value") or {}
    decision_calibration = result.get("decision_calibration") or {}
    scan = result.get("market_scan") or {}
    return {
        "status": result.get("status"),
        "decision": result.get("decision"),
        "selection": result.get("selection"),
        "reason_codes": list(result.get("reason_codes") or []),
        "calibrated_favorite_confidence": decision_calibration.get("calibrated_favorite_confidence"),
        "best_market": {
            "bookmaker": best_market.get("bookmaker"),
            "market_name": best_market.get("market_name"),
            "selected_odd": market.get("selected_odd"),
            "selected_no_vig_probability": market.get("selected_no_vig_probability"),
            "overround": market.get("overround"),
            "edge_percentage_points": value.get("edge_percentage_points"),
            "expected_value_pct": value.get("expected_value_pct"),
            "latest_quote_fetched_at": best_market.get("latest_quote_fetched_at"),
        } if best_market else None,
        "market_scan": {
            "odds_rows_considered": scan.get("odds_rows_considered"),
            "complete_candidate_markets": scan.get("complete_candidate_markets"),
            "rejected_market_groups": scan.get("rejected_market_groups"),
        },
    }


async def run_future_batch(
    *,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    max_fixtures: int = DEFAULT_MAX_FIXTURES,
    min_lead_minutes: int = DEFAULT_MIN_LEAD_MINUTES,
    leagues: list[str] | None = None,
    prediction_window: str = DEFAULT_PREDICTION_WINDOW,
    snapshot_window: str | None = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    lookback_matches: int = DEFAULT_LOOKBACK_MATCHES,
    min_history_matches: int = DEFAULT_MIN_HISTORY_MATCHES,
    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
    max_training_rows: int = DEFAULT_MAX_TRAINING_ROWS,
    class_weight_balanced: bool = False,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_expected_value: float = DEFAULT_MIN_EXPECTED_VALUE,
    min_calibrated_confidence: float = DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    max_overround: float = DEFAULT_MAX_OVERROUND,
    max_quote_span_seconds: int = DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    require_team_favorite_top_class: bool = True,
) -> dict[str, Any]:
    if days_ahead < 0 or days_ahead > MAX_DAYS_AHEAD:
        raise ValueError(f"days_ahead must be between 0 and {MAX_DAYS_AHEAD}")
    if max_fixtures < 1 or max_fixtures > MAX_FIXTURES:
        raise ValueError(f"max_fixtures must be between 1 and {MAX_FIXTURES}")
    if min_lead_minutes < 0 or min_lead_minutes > 1440:
        raise ValueError("min_lead_minutes must be between 0 and 1440")
    if len(str(prediction_window or "")) < 1 or len(str(prediction_window)) > 30:
        raise ValueError("prediction_window must contain 1 to 30 characters")
    if snapshot_window is not None and (len(snapshot_window) < 1 or len(snapshot_window) > 30):
        raise ValueError("snapshot_window must contain 1 to 30 characters when supplied")

    requested_keys = _requested_league_keys(leagues)
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=min_lead_minutes)
    end_date = now.date() + timedelta(days=days_ahead)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    effective_snapshot_window = snapshot_window or f"batch_{now.strftime('%Y%m%dT%H%M%SZ')}"

    client = SportmonksClient()
    fixture_ingestion: list[dict[str, Any]] = []
    day = now.date()
    while day <= end_date:
        try:
            payload = await client.fixtures_by_date(day)
            ingestion = ingest_fixtures_payload(payload)
            fixture_ingestion.append(
                {
                    "date": day.isoformat(),
                    "status": ingestion.get("status"),
                    "received": ingestion.get("received", 0),
                    "created": ingestion.get("created", 0),
                    "updated": ingestion.get("updated", 0),
                    "skipped": ingestion.get("skipped", 0),
                    "error_count": len(ingestion.get("errors") or []),
                }
            )
        except Exception as exc:
            fixture_ingestion.append(
                {
                    "date": day.isoformat(),
                    "status": "upstream_failed",
                    "error": exc.__class__.__name__,
                }
            )
        day += timedelta(days=1)

    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at >= cutoff,
                Fixture.starts_at <= end_dt,
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

        target_candidates: list[Fixture] = []
        for fixture in candidates:
            canonical = canonical_league(fixture.league_name)
            key = canonical.get("key")
            if not canonical.get("target") or not key:
                continue
            if requested_keys and str(key) not in requested_keys:
                continue
            target_candidates.append(fixture)

        selected = target_candidates[:max_fixtures]
        selected_payloads = [_fixture_payload(fixture) for fixture in selected]

    items: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    counts = Counter()

    for fixture_data in selected_payloads:
        sportmonks_fixture_id = int(fixture_data["sportmonks_fixture_id"])
        item: dict[str, Any] = {
            "fixture": fixture_data,
            "inference": None,
            "odds": None,
            "decision": None,
        }

        try:
            inference = generate_and_persist_prematch_prediction(
                sportmonks_fixture_id=sportmonks_fixture_id,
                prediction_window=prediction_window,
                history_days=history_days,
                lookback_matches=lookback_matches,
                min_history_matches=min_history_matches,
                min_training_rows=min_training_rows,
                max_training_rows=max_training_rows,
                class_weight_balanced=class_weight_balanced,
            )
            inference_status = str(inference.get("status") or "unknown")
            item["inference"] = {
                "status": inference_status,
                "reason_codes": list(inference.get("reason_codes") or []),
                "prediction": inference.get("prediction"),
                "target_feature_audit": inference.get("target_feature_audit"),
                "training_audit": inference.get("training_audit"),
            }
            if inference_status in {"ok", "exists"}:
                counts["inference_ready"] += 1
            else:
                counts["inference_not_ready"] += 1
                for reason in inference.get("reason_codes") or []:
                    reason_counts[str(reason)] += 1
                items.append(item)
                continue
        except Exception as exc:
            counts["failed"] += 1
            reason_counts["INFERENCE_EXCEPTION"] += 1
            item["inference"] = {
                "status": "failed",
                "error": exc.__class__.__name__,
            }
            items.append(item)
            continue

        try:
            odds_payload = await client.prematch_odds_by_fixture(sportmonks_fixture_id)
            odds_result = ingest_prematch_odds_payload(
                sportmonks_fixture_id=sportmonks_fixture_id,
                payload=odds_payload,
                snapshot_window=effective_snapshot_window,
            )
            item["odds"] = {
                "status": odds_result.get("status"),
                "received": odds_result.get("received", 0),
                "created": odds_result.get("created", 0),
                "filtered_out": odds_result.get("filtered_out", 0),
                "skipped": odds_result.get("skipped", 0),
                "error_count": len(odds_result.get("errors") or []),
                "snapshot_window": effective_snapshot_window,
            }
            if odds_result.get("status") == "ok":
                counts["odds_ingested"] += 1
                counts["odds_rows_created"] += int(odds_result.get("created") or 0)
            else:
                counts["odds_not_ready"] += 1
        except Exception as exc:
            counts["odds_failed"] += 1
            reason_counts["ODDS_UPSTREAM_EXCEPTION"] += 1
            item["odds"] = {
                "status": "upstream_failed",
                "error": exc.__class__.__name__,
                "snapshot_window": effective_snapshot_window,
            }

        try:
            decision = evaluate_fixture_decision(
                sportmonks_fixture_id=sportmonks_fixture_id,
                prediction_window=prediction_window,
                model_version=MODEL_VERSION,
                snapshot_window=effective_snapshot_window,
                min_edge=min_edge,
                min_expected_value=min_expected_value,
                min_calibrated_confidence=min_calibrated_confidence,
                max_overround=max_overround,
                max_quote_span_seconds=max_quote_span_seconds,
                require_team_favorite_top_class=require_team_favorite_top_class,
                include_market_candidates=False,
            )
            compact = _compact_decision(decision)
            item["decision"] = compact
            decision_value = str(compact.get("decision") or "")
            if decision.get("status") == "ok":
                counts["decisions_evaluated"] += 1
                if decision_value == "BET":
                    counts["bet"] += 1
                elif decision_value == "NO_BET":
                    counts["no_bet"] += 1
            else:
                counts["decision_not_ready"] += 1
            for reason in compact.get("reason_codes") or []:
                reason_counts[str(reason)] += 1
        except Exception as exc:
            counts["failed"] += 1
            reason_counts["DECISION_EXCEPTION"] += 1
            item["decision"] = {
                "status": "failed",
                "decision": "NO_BET",
                "reason_codes": ["DECISION_EXCEPTION"],
                "error": exc.__class__.__name__,
            }

        items.append(item)

    run_hash = _stable_hash(
        {
            "version": FUTURE_BATCH_VERSION,
            "evaluated_at": now.isoformat(),
            "prediction_window": prediction_window,
            "snapshot_window": effective_snapshot_window,
            "selected_fixture_ids": [
                item["fixture"]["sportmonks_fixture_id"] for item in items
            ],
            "thresholds": {
                "min_edge": min_edge,
                "min_expected_value": min_expected_value,
                "min_calibrated_confidence": min_calibrated_confidence,
                "max_overround": max_overround,
            },
        }
    )

    return {
        "status": "ok",
        "version": FUTURE_BATCH_VERSION,
        "run_id": f"{FUTURE_BATCH_VERSION}:{run_hash[:16]}",
        "evaluated_at": now.isoformat(),
        "horizon": {
            "days_ahead": days_ahead,
            "min_lead_minutes": min_lead_minutes,
            "cutoff": cutoff.isoformat(),
            "end_at": end_dt.isoformat(),
            "league_filters": leagues or [],
            "max_fixtures": max_fixtures,
        },
        "windows": {
            "prediction_window": prediction_window,
            "snapshot_window": effective_snapshot_window,
        },
        "fixture_ingestion": fixture_ingestion,
        "summary": {
            "future_target_candidates": len(target_candidates),
            "selected_fixtures": len(selected_payloads),
            "inference_ready": counts["inference_ready"],
            "inference_not_ready": counts["inference_not_ready"],
            "odds_ingested": counts["odds_ingested"],
            "odds_rows_created": counts["odds_rows_created"],
            "odds_not_ready": counts["odds_not_ready"],
            "odds_failed": counts["odds_failed"],
            "decisions_evaluated": counts["decisions_evaluated"],
            "bet": counts["bet"],
            "no_bet": counts["no_bet"],
            "decision_not_ready": counts["decision_not_ready"],
            "failed": counts["failed"],
            "reason_code_counts": dict(sorted(reason_counts.items())),
        },
        "items": items,
        "policy": {
            "execution_mode": "RESEARCH_ONLY",
            "auto_execution": False,
            "stake_sizing_enabled": False,
            "real_money_execution_enabled": False,
            "future_fixtures_only": True,
            "minimum_lead_time_enforced": True,
            "prediction_immutability_preserved": True,
            "dynamic_snapshot_window_prevents_stale_odds_reuse": snapshot_window is None,
            "decision_thresholds_are_initial_not_test_optimized": True,
            "max_batch_size": MAX_FIXTURES,
        },
    }


@router.post("/research/future-batch/run")
async def future_batch_run_endpoint(
    days_ahead: int = Query(default=DEFAULT_DAYS_AHEAD, ge=0, le=MAX_DAYS_AHEAD),
    max_fixtures: int = Query(default=DEFAULT_MAX_FIXTURES, ge=1, le=MAX_FIXTURES),
    min_lead_minutes: int = Query(default=DEFAULT_MIN_LEAD_MINUTES, ge=0, le=1440),
    leagues: list[str] | None = Query(default=None),
    prediction_window: str = Query(default=DEFAULT_PREDICTION_WINDOW, min_length=1, max_length=30),
    snapshot_window: str | None = Query(default=None, min_length=1, max_length=30),
    history_days: int = Query(default=DEFAULT_HISTORY_DAYS, ge=90, le=3650),
    lookback_matches: int = Query(default=DEFAULT_LOOKBACK_MATCHES, ge=1, le=10),
    min_history_matches: int = Query(default=DEFAULT_MIN_HISTORY_MATCHES, ge=1, le=10),
    min_training_rows: int = Query(default=DEFAULT_MIN_TRAINING_ROWS, ge=60, le=5000),
    max_training_rows: int = Query(default=DEFAULT_MAX_TRAINING_ROWS, ge=100, le=5000),
    class_weight_balanced: bool = False,
    min_edge: float = Query(default=DEFAULT_MIN_EDGE, ge=0.0, le=0.30),
    min_expected_value: float = Query(default=DEFAULT_MIN_EXPECTED_VALUE, ge=0.0, le=0.50),
    min_calibrated_confidence: float = Query(default=DEFAULT_MIN_CALIBRATED_CONFIDENCE, ge=0.30, le=0.80),
    max_overround: float = Query(default=DEFAULT_MAX_OVERROUND, ge=0.0, le=0.30),
    max_quote_span_seconds: int = Query(default=DEFAULT_MAX_QUOTE_SPAN_SECONDS, ge=0, le=3600),
    require_team_favorite_top_class: bool = True,
) -> dict[str, Any]:
    try:
        return await run_future_batch(
            days_ahead=days_ahead,
            max_fixtures=max_fixtures,
            min_lead_minutes=min_lead_minutes,
            leagues=leagues,
            prediction_window=prediction_window,
            snapshot_window=snapshot_window,
            history_days=history_days,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            min_training_rows=min_training_rows,
            max_training_rows=max_training_rows,
            class_weight_balanced=class_weight_balanced,
            min_edge=min_edge,
            min_expected_value=min_expected_value,
            min_calibrated_confidence=min_calibrated_confidence,
            max_overround=max_overround,
            max_quote_span_seconds=max_quote_span_seconds,
            require_team_favorite_top_class=require_team_favorite_top_class,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
