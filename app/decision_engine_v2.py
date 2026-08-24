from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.decision_engine import (
    DEFAULT_MAX_OVERROUND,
    DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    DEFAULT_MIN_EXPECTED_VALUE,
    evaluate_fixture_decision,
)

DECISION_ENGINE_V2_VERSION = "decision_engine_v2"

router = APIRouter(prefix="/decision", tags=["decision"])


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


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_rank_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    value = candidate.get("value") or {}
    market = candidate.get("market") or {}
    # A candidate that passes every existing decision gate must outrank a
    # candidate that fails a gate. Inside the same decision class, keep the
    # original economic preference: higher EV, then higher edge. Lower
    # overround and stable names are deterministic tie-breakers only.
    return (
        0 if candidate.get("decision") == "BET" else 1,
        -_as_float(value.get("expected_value_decimal"), -999.0),
        -_as_float(value.get("edge_probability_points"), -999.0),
        _as_float(market.get("overround"), 999.0),
        str(candidate.get("bookmaker") or ""),
        str(candidate.get("market_name") or ""),
    )


def _candidate_timing_audit(
    candidate: dict[str, Any],
    *,
    quote_not_before: datetime | None,
    quote_not_after: datetime | None,
) -> dict[str, Any]:
    latest = _parse_datetime(candidate.get("latest_quote_fetched_at"))
    span_seconds = max(0.0, _as_float(candidate.get("quote_span_seconds"), 0.0))
    earliest = latest - timedelta(seconds=span_seconds) if latest is not None else None

    reasons: list[str] = []
    if quote_not_before is not None:
        if earliest is None or earliest < quote_not_before:
            reasons.append("QUOTE_BEFORE_REQUIRED_WINDOW")
    if quote_not_after is not None:
        if latest is None or latest >= quote_not_after:
            reasons.append("QUOTE_AFTER_REQUIRED_WINDOW")

    return {
        "eligible": not reasons,
        "reason_codes": reasons,
        "earliest_quote_fetched_at": earliest.isoformat() if earliest else None,
        "latest_quote_fetched_at": latest.isoformat() if latest else None,
    }


def _apply_candidate_to_response(
    response: dict[str, Any],
    best: dict[str, Any],
) -> None:
    response["decision"] = best.get("decision")
    response["selection"] = best.get("selection")
    response["reason_codes"] = list(best.get("reason_codes") or [])
    response["best_market"] = {
        "bookmaker": best.get("bookmaker"),
        "market_name": best.get("market_name"),
        "snapshot_window": best.get("snapshot_window"),
        "quote_span_seconds": best.get("quote_span_seconds"),
        "latest_quote_fetched_at": best.get("latest_quote_fetched_at"),
        "market": best.get("market") or {},
        "value": best.get("value") or {},
        "thresholds": best.get("thresholds") or {},
    }
    response["decision_model"] = best.get("model") or {}
    response["decision_calibration"] = best.get("calibration") or {}


def evaluate_fixture_decision_v2(
    *,
    sportmonks_fixture_id: int,
    prediction_window: str | None = None,
    model_version: str | None = None,
    snapshot_window: str | None = None,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_expected_value: float = DEFAULT_MIN_EXPECTED_VALUE,
    min_calibrated_confidence: float = DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    max_overround: float = DEFAULT_MAX_OVERROUND,
    max_quote_span_seconds: int = DEFAULT_MAX_QUOTE_SPAN_SECONDS,
    require_team_favorite_top_class: bool = True,
    quote_not_before: datetime | None = None,
    quote_not_after: datetime | None = None,
    include_market_candidates: bool = False,
) -> dict[str, Any]:
    quote_not_before = _aware_utc(quote_not_before) if quote_not_before else None
    quote_not_after = _aware_utc(quote_not_after) if quote_not_after else None
    if quote_not_before and quote_not_after and quote_not_before >= quote_not_after:
        raise ValueError("quote_not_before must be before quote_not_after")

    original = evaluate_fixture_decision(
        sportmonks_fixture_id=sportmonks_fixture_id,
        prediction_window=prediction_window,
        model_version=model_version,
        snapshot_window=snapshot_window,
        min_edge=min_edge,
        min_expected_value=min_expected_value,
        min_calibrated_confidence=min_calibrated_confidence,
        max_overround=max_overround,
        max_quote_span_seconds=max_quote_span_seconds,
        require_team_favorite_top_class=require_team_favorite_top_class,
        include_market_candidates=True,
    )
    response = deepcopy(original)
    response["version"] = DECISION_ENGINE_V2_VERSION

    policy = dict(response.get("policy") or {})
    policy.update(
        {
            "valid_bet_candidate_prioritized_over_failing_candidate": True,
            "candidate_order_after_gates": "BET_FIRST_THEN_EV_EDGE_OVERROUND",
            "decision_thresholds_unchanged_from_v1": True,
            "best_complete_bookmaker_market_selected_by_expected_value": False,
        }
    )
    response["policy"] = policy

    if original.get("status") != "ok":
        if not include_market_candidates:
            response.pop("market_candidates", None)
            response.pop("rejected_market_groups", None)
        return response

    candidates = list(original.get("market_candidates") or [])
    eligible: list[dict[str, Any]] = []
    timing_rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        timing = _candidate_timing_audit(
            candidate,
            quote_not_before=quote_not_before,
            quote_not_after=quote_not_after,
        )
        if timing["eligible"]:
            enriched = dict(candidate)
            enriched["timing_audit"] = timing
            eligible.append(enriched)
        else:
            timing_rejected.append(
                {
                    "bookmaker": candidate.get("bookmaker"),
                    "market_name": candidate.get("market_name"),
                    "snapshot_window": candidate.get("snapshot_window"),
                    **timing,
                }
            )

    market_scan = dict(response.get("market_scan") or {})
    market_scan.update(
        {
            "eligible_candidate_markets_after_timing": len(eligible),
            "timing_rejected_market_groups": len(timing_rejected),
            "bet_candidate_markets": sum(1 for item in eligible if item.get("decision") == "BET"),
        }
    )
    response["market_scan"] = market_scan
    response["quote_timing_policy"] = {
        "quote_not_before": quote_not_before.isoformat() if quote_not_before else None,
        "quote_not_after": quote_not_after.isoformat() if quote_not_after else None,
        "all_three_1x2_quotes_must_fit_window": bool(quote_not_before or quote_not_after),
        "method": "earliest quote = latest quote - quote span",
    }

    if not eligible:
        response["status"] = "not_ready"
        response["decision"] = "NO_BET"
        response["selection"] = None
        response["reason_codes"] = ["COMPLETE_1X2_ODDS_NOT_AVAILABLE_IN_REQUIRED_WINDOW"]
        response.pop("best_market", None)
        response.pop("decision_model", None)
        response.pop("decision_calibration", None)
        if include_market_candidates:
            response["market_candidates"] = []
            response["timing_rejected_market_groups"] = timing_rejected[:20]
        else:
            response.pop("market_candidates", None)
            response.pop("rejected_market_groups", None)
        return response

    eligible.sort(key=_candidate_rank_key)
    best = eligible[0]
    _apply_candidate_to_response(response, best)

    if include_market_candidates:
        response["market_candidates"] = eligible[:20]
        response["timing_rejected_market_groups"] = timing_rejected[:20]
    else:
        response.pop("market_candidates", None)
        response.pop("rejected_market_groups", None)

    return response


@router.get("/fixture/{sportmonks_fixture_id}/v2")
def decision_fixture_v2_endpoint(
    sportmonks_fixture_id: int,
    prediction_window: str | None = Query(default=None),
    model_version: str | None = Query(default=None),
    snapshot_window: str | None = Query(default=None),
    min_edge: float = Query(default=DEFAULT_MIN_EDGE, ge=0.0, le=1.0),
    min_expected_value: float = Query(default=DEFAULT_MIN_EXPECTED_VALUE, ge=-1.0, le=10.0),
    min_calibrated_confidence: float = Query(
        default=DEFAULT_MIN_CALIBRATED_CONFIDENCE,
        ge=0.0,
        le=1.0,
    ),
    max_overround: float = Query(default=DEFAULT_MAX_OVERROUND, ge=0.0, le=1.0),
    max_quote_span_seconds: int = Query(
        default=DEFAULT_MAX_QUOTE_SPAN_SECONDS,
        ge=0,
        le=3600,
    ),
    require_team_favorite_top_class: bool = True,
    quote_not_before: datetime | None = Query(default=None),
    quote_not_after: datetime | None = Query(default=None),
    include_market_candidates: bool = False,
) -> dict[str, Any]:
    try:
        return evaluate_fixture_decision_v2(
            sportmonks_fixture_id=sportmonks_fixture_id,
            prediction_window=prediction_window,
            model_version=model_version,
            snapshot_window=snapshot_window,
            min_edge=min_edge,
            min_expected_value=min_expected_value,
            min_calibrated_confidence=min_calibrated_confidence,
            max_overround=max_overround,
            max_quote_span_seconds=max_quote_span_seconds,
            require_team_favorite_top_class=require_team_favorite_top_class,
            quote_not_before=quote_not_before,
            quote_not_after=quote_not_after,
            include_market_candidates=include_market_candidates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
