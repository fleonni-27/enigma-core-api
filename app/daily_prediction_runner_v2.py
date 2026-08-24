from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app import daily_prediction_runner as legacy
from app.database import SessionLocal
from app.decision_engine import (
    DEFAULT_MAX_OVERROUND,
    DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_EXPECTED_VALUE,
)
from app.decision_engine_v2 import evaluate_fixture_decision_v2
from app.forward_test_ledger import (
    DecisionRecord,
    ensure_forward_test_schema,
    persist_evaluated_decision,
)
from app.inference_runtime_v2 import INFERENCE_RUNTIME_VERSION, InferenceRuntimeV2
from app.prematch_inference import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_LOOKBACK_MATCHES,
    DEFAULT_MAX_TRAINING_ROWS,
    DEFAULT_MIN_HISTORY_MATCHES,
    DEFAULT_MIN_TRAINING_ROWS,
    MODEL_VERSION,
)
from app.sportmonks import SportmonksClient

DAILY_PREDICTION_RUNNER_VERSION = "daily_prediction_runner_v2"
LEDGER_SOURCE_VERSION = legacy.DAILY_PREDICTION_RUNNER_VERSION
BUSINESS_TIMEZONE = legacy.BUSINESS_TIMEZONE
J1_TARGET_LEAD_MINUTES = legacy.J1_TARGET_LEAD_MINUTES
J1_PREDICTION_WINDOW = legacy.J1_PREDICTION_WINDOW
J1_SNAPSHOT_PREFIX = legacy.J1_SNAPSHOT_PREFIX
DEFAULT_MAX_LATENESS_MINUTES = legacy.DEFAULT_MAX_LATENESS_MINUTES
DEFAULT_MAX_FIXTURES = legacy.DEFAULT_MAX_FIXTURES
MAX_FIXTURES_PER_RUN = legacy.MAX_FIXTURES_PER_RUN
DEFAULT_J1_UPSTREAM_CONCURRENCY = 4

router = APIRouter(prefix="/operations", tags=["operations"])


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _prediction_timing_audit(
    *,
    fixture,
    inference: dict[str, Any],
) -> dict[str, Any]:
    starts_at = _aware_utc(fixture.starts_at)
    j1_due_at = starts_at - timedelta(minutes=J1_TARGET_LEAD_MINUTES)
    prediction = inference.get("prediction") or {}
    generated_at = _parse_datetime(prediction.get("generated_at"))

    reason_codes: list[str] = []
    if generated_at is None:
        reason_codes.append("PREDICTION_GENERATED_AT_MISSING")
    else:
        if generated_at < j1_due_at:
            reason_codes.append("PREDICTION_GENERATED_BEFORE_J1_DUE")
        if generated_at >= starts_at:
            reason_codes.append("PREDICTION_GENERATED_AT_OR_AFTER_KICKOFF")

    return {
        "valid": not reason_codes,
        "reason_codes": reason_codes,
        "j1_due_at": j1_due_at.isoformat(),
        "kickoff_at": starts_at.isoformat(),
        "prediction_generated_at": generated_at.isoformat() if generated_at else None,
        "minutes_after_j1_due": (
            round((generated_at - j1_due_at).total_seconds() / 60.0, 3)
            if generated_at is not None
            else None
        ),
    }


def _compact_decision(result: dict[str, Any]) -> dict[str, Any]:
    best_market = result.get("best_market") or {}
    market = best_market.get("market") or {}
    value = best_market.get("value") or {}
    calibration = result.get("decision_calibration") or {}
    return {
        "status": result.get("status"),
        "version": result.get("version"),
        "decision": result.get("decision"),
        "selection": result.get("selection"),
        "reason_codes": list(result.get("reason_codes") or []),
        "calibrated_favorite_confidence": calibration.get("calibrated_favorite_confidence"),
        "bookmaker": best_market.get("bookmaker"),
        "market_name": best_market.get("market_name"),
        "selected_odd": market.get("selected_odd"),
        "selected_no_vig_probability": market.get("selected_no_vig_probability"),
        "edge_percentage_points": value.get("edge_percentage_points"),
        "expected_value_pct": value.get("expected_value_pct"),
        "market_scan": result.get("market_scan") or {},
        "quote_timing_policy": result.get("quote_timing_policy") or {},
    }


def _run_health(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            "status": "IDLE",
            "reason_codes": ["NO_J1_FIXTURES_DUE"],
            "fatal_items": 0,
            "degraded_items": 0,
        }

    fatal_statuses = {
        "prediction_timing_invalid",
        "inference_failed",
        "decision_failed",
        "ledger_not_ready",
    }
    degraded_statuses = {
        "inference_not_ready",
        "decision_not_ready",
    }
    fatal = [item for item in items if item.get("status") in fatal_statuses]
    degraded = [item for item in items if item.get("status") in degraded_statuses]

    if fatal:
        status = "FAILED"
        reasons = sorted({str(item.get("status")) for item in fatal})
    elif degraded:
        status = "DEGRADED"
        reasons = sorted({str(item.get("status")) for item in degraded})
    else:
        status = "OK"
        reasons = []

    return {
        "status": status,
        "reason_codes": reasons,
        "fatal_items": len(fatal),
        "degraded_items": len(degraded),
    }


def _recorded_fixture_windows(fixtures: list[Any]) -> set[tuple[int, str]]:
    if not fixtures:
        return set()

    ensure_forward_test_schema()
    fixture_ids = [int(fixture.id) for fixture in fixtures]
    with SessionLocal() as session:
        rows = session.execute(
            select(DecisionRecord.fixture_id, DecisionRecord.snapshot_window).where(
                DecisionRecord.fixture_id.in_(fixture_ids),
                DecisionRecord.source == LEDGER_SOURCE_VERSION,
            )
        ).all()
    return {(int(fixture_id), str(snapshot_window)) for fixture_id, snapshot_window in rows}


async def _fetch_j1_upstream(
    client: SportmonksClient,
    fixtures: list[Any],
    *,
    concurrency: int = DEFAULT_J1_UPSTREAM_CONCURRENCY,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    if concurrency < 1 or concurrency > 12:
        raise ValueError("concurrency must be between 1 and 12")

    semaphore = asyncio.Semaphore(concurrency)
    cycle_started = perf_counter()

    async def request(kind: str, fixture_id: int) -> dict[str, Any]:
        async with semaphore:
            started = perf_counter()
            try:
                if kind == "enriched":
                    payload = await client.enriched_fixture(fixture_id)
                else:
                    payload = await client.prematch_odds_by_fixture(fixture_id)
                return {
                    "status": "ok",
                    "payload": payload,
                    "fetch_seconds": round(perf_counter() - started, 6),
                }
            except Exception as exc:
                return {
                    "status": "upstream_failed",
                    "error": exc.__class__.__name__,
                    "fetch_seconds": round(perf_counter() - started, 6),
                }

    async def fetch_fixture(fixture: Any) -> tuple[int, dict[str, Any]]:
        fixture_id = int(fixture.sportmonks_id)
        enriched, odds = await asyncio.gather(
            request("enriched", fixture_id),
            request("odds", fixture_id),
        )
        return fixture_id, {"enriched": enriched, "odds": odds}

    pairs = await asyncio.gather(*(fetch_fixture(fixture) for fixture in fixtures))
    payloads = dict(pairs)
    return payloads, {
        "concurrency": concurrency,
        "fixture_count": len(fixtures),
        "request_count": len(fixtures) * 2,
        "prefetch_seconds": round(perf_counter() - cycle_started, 6),
    }


async def run_daily_prediction_runner(
    *,
    max_lateness_minutes: int = DEFAULT_MAX_LATENESS_MINUTES,
    max_fixtures: int = DEFAULT_MAX_FIXTURES,
) -> dict[str, Any]:
    if max_lateness_minutes < 1 or max_lateness_minutes > 30:
        raise ValueError("max_lateness_minutes must be between 1 and 30")
    if max_fixtures < 1 or max_fixtures > MAX_FIXTURES_PER_RUN:
        raise ValueError(f"max_fixtures must be between 1 and {MAX_FIXTURES_PER_RUN}")

    cycle_started = perf_counter()
    now = datetime.now(timezone.utc)
    fixtures = legacy._due_target_fixtures(
        now=now,
        max_lateness_minutes=max_lateness_minutes,
        max_fixtures=max_fixtures,
    )
    inference_runtime = InferenceRuntimeV2(
        history_days=DEFAULT_HISTORY_DAYS,
        lookback_matches=DEFAULT_LOOKBACK_MATCHES,
        min_history_matches=DEFAULT_MIN_HISTORY_MATCHES,
        min_training_rows=DEFAULT_MIN_TRAINING_ROWS,
        max_training_rows=DEFAULT_MAX_TRAINING_ROWS,
        class_weight_balanced=False,
    )
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    recorded = _recorded_fixture_windows(fixtures)
    pending_fixtures = [
        fixture
        for fixture in fixtures
        if (int(fixture.id), legacy._snapshot_window(fixture)) not in recorded
    ]

    upstream: dict[int, dict[str, Any]] = {}
    upstream_audit: dict[str, Any] = {
        "concurrency": DEFAULT_J1_UPSTREAM_CONCURRENCY,
        "fixture_count": 0,
        "request_count": 0,
        "prefetch_seconds": 0.0,
    }
    transport_audit: dict[str, Any] = {}

    if pending_fixtures:
        async with SportmonksClient() as client:
            upstream, upstream_audit = await _fetch_j1_upstream(
                client,
                pending_fixtures,
                concurrency=DEFAULT_J1_UPSTREAM_CONCURRENCY,
            )
            transport_audit = client.transport_audit()

    for fixture in fixtures:
        snapshot_window = legacy._snapshot_window(fixture)
        fixture_data = legacy._fixture_payload(fixture, now)
        starts_at = _aware_utc(fixture.starts_at)
        j1_due_at = starts_at - timedelta(minutes=J1_TARGET_LEAD_MINUTES)
        item: dict[str, Any] = {
            "fixture": fixture_data,
            "snapshot_window": snapshot_window,
            "lineup_context": None,
            "odds": None,
            "inference": None,
            "prediction_timing_audit": None,
            "decision": None,
            "ledger": None,
        }

        if (int(fixture.id), snapshot_window) in recorded:
            counts["already_recorded"] += 1
            item["status"] = "already_recorded"
            items.append(item)
            continue

        fetched = upstream.get(int(fixture.sportmonks_id), {})
        enriched_fetch = fetched.get("enriched") or {}
        if enriched_fetch.get("status") == "ok":
            try:
                item["lineup_context"] = legacy._persist_lineup_context(
                    fixture=fixture,
                    snapshot_window=snapshot_window,
                    payload=enriched_fetch.get("payload") or {},
                )
                item["lineup_context"]["fetch_seconds"] = enriched_fetch.get("fetch_seconds")
                if item["lineup_context"].get("lineups_available"):
                    counts["lineups_available"] += 1
                else:
                    counts["lineups_not_available"] += 1
            except Exception as exc:
                counts["lineup_fetch_failed"] += 1
                item["lineup_context"] = {
                    "status": "persistence_failed",
                    "error": exc.__class__.__name__,
                    "fetch_seconds": enriched_fetch.get("fetch_seconds"),
                    "used_by_current_model": False,
                }
        else:
            counts["lineup_fetch_failed"] += 1
            item["lineup_context"] = {
                "status": "upstream_failed",
                "error": enriched_fetch.get("error") or "MISSING_PREFETCH_RESULT",
                "fetch_seconds": enriched_fetch.get("fetch_seconds"),
                "used_by_current_model": False,
            }

        odds_fetch = fetched.get("odds") or {}
        if odds_fetch.get("status") == "ok":
            try:
                odds_result = legacy.ingest_prematch_odds_payload(
                    sportmonks_fixture_id=int(fixture.sportmonks_id),
                    payload=odds_fetch.get("payload") or {},
                    snapshot_window=snapshot_window,
                )
                item["odds"] = {
                    "status": odds_result.get("status"),
                    "received": odds_result.get("received", 0),
                    "created": odds_result.get("created", 0),
                    "filtered_out": odds_result.get("filtered_out", 0),
                    "skipped": odds_result.get("skipped", 0),
                    "error_count": len(odds_result.get("errors") or []),
                    "fetch_seconds": odds_fetch.get("fetch_seconds"),
                }
                counts["odds_rows_created"] += int(odds_result.get("created") or 0)
            except Exception as exc:
                counts["odds_failed"] += 1
                item["odds"] = {
                    "status": "ingestion_failed",
                    "error": exc.__class__.__name__,
                    "fetch_seconds": odds_fetch.get("fetch_seconds"),
                }
        else:
            counts["odds_failed"] += 1
            item["odds"] = {
                "status": "upstream_failed",
                "error": odds_fetch.get("error") or "MISSING_PREFETCH_RESULT",
                "fetch_seconds": odds_fetch.get("fetch_seconds"),
            }

        try:
            inference = inference_runtime.generate_and_persist_prediction(
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                prediction_window=J1_PREDICTION_WINDOW,
            )
            item["inference"] = {
                "status": inference.get("status"),
                "runtime_version": inference.get("runtime_version"),
                "reason_codes": list(inference.get("reason_codes") or []),
                "prediction": inference.get("prediction"),
                "target_feature_audit": inference.get("target_feature_audit"),
                "training_audit": inference.get("training_audit"),
            }
            if inference.get("status") not in {"ok", "exists"}:
                counts["inference_not_ready"] += 1
                item["status"] = "inference_not_ready"
                items.append(item)
                continue

            timing_audit = _prediction_timing_audit(fixture=fixture, inference=inference)
            item["prediction_timing_audit"] = timing_audit
            if not timing_audit["valid"]:
                counts["prediction_timing_invalid"] += 1
                item["status"] = "prediction_timing_invalid"
                items.append(item)
                continue

            counts["inference_ready"] += 1
        except Exception as exc:
            counts["inference_failed"] += 1
            item["inference"] = {
                "status": "failed",
                "runtime_version": INFERENCE_RUNTIME_VERSION,
                "error": exc.__class__.__name__,
            }
            item["status"] = "inference_failed"
            items.append(item)
            continue

        try:
            decision = evaluate_fixture_decision_v2(
                sportmonks_fixture_id=int(fixture.sportmonks_id),
                prediction_window=J1_PREDICTION_WINDOW,
                model_version=MODEL_VERSION,
                snapshot_window=snapshot_window,
                min_edge=DEFAULT_MIN_EDGE,
                min_expected_value=DEFAULT_MIN_EXPECTED_VALUE,
                min_calibrated_confidence=DEFAULT_MIN_CALIBRATED_CONFIDENCE,
                max_overround=DEFAULT_MAX_OVERROUND,
                max_quote_span_seconds=DEFAULT_MAX_QUOTE_SPAN_SECONDS,
                require_team_favorite_top_class=True,
                quote_not_before=j1_due_at,
                quote_not_after=starts_at,
                include_market_candidates=False,
            )
            item["decision"] = _compact_decision(decision)
            if decision.get("status") != "ok":
                counts["decision_not_ready"] += 1
                item["status"] = "decision_not_ready"
                items.append(item)
                continue

            counts["decisions_evaluated"] += 1
            if decision.get("decision") == "BET":
                counts["bet"] += 1
            elif decision.get("decision") == "NO_BET":
                counts["no_bet"] += 1

            ledger = persist_evaluated_decision(
                decision,
                source=LEDGER_SOURCE_VERSION,
            )
            item["ledger"] = {
                "status": ledger.get("status"),
                "reason_codes": list(ledger.get("reason_codes") or []),
                "record": ledger.get("record"),
            }
            if ledger.get("status") in {"persisted", "exists"}:
                counts["ledger_ready"] += 1
                item["status"] = "completed"
            else:
                counts["ledger_not_ready"] += 1
                item["status"] = "ledger_not_ready"
        except Exception as exc:
            counts["decision_failed"] += 1
            item["decision"] = {"status": "failed", "error": exc.__class__.__name__}
            item["status"] = "decision_failed"

        items.append(item)

    health = _run_health(items)
    runtime_audit = inference_runtime.audit()
    return {
        "status": "ok",
        "version": DAILY_PREDICTION_RUNNER_VERSION,
        "ledger_source_version": LEDGER_SOURCE_VERSION,
        "evaluated_at": now.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "run_health": health,
        "inference_runtime": runtime_audit,
        "performance": {
            "cycle_seconds": round(perf_counter() - cycle_started, 6),
            "recorded_decision_lookup_queries": 1 if fixtures else 0,
            "recorded_decision_lookup_batched": True,
            "pending_fixture_count": len(pending_fixtures),
            "upstream_prefetch": upstream_audit,
            "sportmonks_transport": transport_audit,
            "upstream_network_parallel_persistence_and_inference_sequential": True,
        },
        "window": {
            "name": "J1",
            "target_lead_minutes": J1_TARGET_LEAD_MINUTES,
            "execution_rule": "never early; first scheduler run at or after kickoff-45m",
            "max_lateness_minutes": max_lateness_minutes,
            "prediction_window": J1_PREDICTION_WINDOW,
        },
        "selected_fixtures": len(fixtures),
        "counts": dict(counts),
        "items": items,
        "policy": {
            "target_leagues_only": True,
            "prediction_is_immutable_once_persisted": True,
            "prediction_must_be_generated_at_or_after_j1_due": True,
            "historical_dataset_reused_within_same_j1_cycle": True,
            "per_fixture_temporal_cutoff_preserved": True,
            "model_fit_reused_only_for_identical_training_sha256": True,
            "distinct_training_sha256_never_share_fit": True,
            "target_features_remain_per_fixture": True,
            "all_selected_1x2_quotes_must_fit_j1_window": True,
            "valid_bet_candidate_is_prioritized_over_market_that_fails_a_gate": True,
            "decision_thresholds_unchanged": True,
            "decision_is_persisted_pre_kickoff": True,
            "odds_snapshot_is_j1_specific": True,
            "lineups_are_captured_in_separate_prematch_context_table": True,
            "pregame_lineups_do_not_pollute_postgame_training_snapshots": True,
            "lineups_used_by_current_standard_model": False,
            "current_standard_model_remains_36_features": True,
            "research_only": True,
            "auto_betting": False,
        },
    }


def build_j1_status(*, target_date: date | None = None) -> dict[str, Any]:
    payload = legacy.build_j1_status(target_date=target_date)
    payload["version"] = DAILY_PREDICTION_RUNNER_VERSION
    payload["ledger_source_version"] = LEDGER_SOURCE_VERSION
    payload["decision_engine_version"] = "decision_engine_v2"
    payload["inference_runtime_version"] = INFERENCE_RUNTIME_VERSION
    return payload


@router.post("/daily-prediction-runner")
async def daily_prediction_runner_endpoint(
    max_lateness_minutes: int = Query(default=DEFAULT_MAX_LATENESS_MINUTES, ge=1, le=30),
    max_fixtures: int = Query(default=DEFAULT_MAX_FIXTURES, ge=1, le=MAX_FIXTURES_PER_RUN),
) -> dict[str, Any]:
    try:
        return await run_daily_prediction_runner(
            max_lateness_minutes=max_lateness_minutes,
            max_fixtures=max_fixtures,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc


@router.get("/daily-prediction-runner/status")
def daily_prediction_runner_status_endpoint(
    target_date: date | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return build_j1_status(target_date=target_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
