from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.database import SessionLocal
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.odds_window_clv import CLVRecord

FORWARD_TEST_REPORT_VERSION = "forward_test_report_v2"
BUSINESS_TIMEZONE = "America/Sao_Paulo"
DEFAULT_MAX_REPORT_RECORDS = 5000
MAX_REPORT_RECORDS = 20000
PROBABILITY_EPSILON = 1e-12

EDGE_BUCKET_ORDER = (
    "<0pp",
    "0-<2.5pp",
    "2.5-<5pp",
    "5-<7.5pp",
    "7.5-<10pp",
    ">=10pp",
    "UNAVAILABLE",
)
CONFIDENCE_BUCKET_ORDER = (
    "<45%",
    "45-<50%",
    "50-<55%",
    "55-<60%",
    "60-<65%",
    ">=65%",
    "UNAVAILABLE",
)

router = APIRouter(prefix="/research/forward-test", tags=["research"])
Pair = tuple[DecisionRecord, CLVRecord | None]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe_rate(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None else None


def _edge_bucket(value: Any) -> str:
    edge = _as_float(value)
    if edge is None:
        return "UNAVAILABLE"
    if edge < 0:
        return "<0pp"
    if edge < 2.5:
        return "0-<2.5pp"
    if edge < 5.0:
        return "2.5-<5pp"
    if edge < 7.5:
        return "5-<7.5pp"
    if edge < 10.0:
        return "7.5-<10pp"
    return ">=10pp"


def _confidence_bucket(value: Any) -> str:
    confidence = _as_float(value)
    if confidence is None:
        return "UNAVAILABLE"
    if confidence < 0.45:
        return "<45%"
    if confidence < 0.50:
        return "45-<50%"
    if confidence < 0.55:
        return "50-<55%"
    if confidence < 0.60:
        return "55-<60%"
    if confidence < 0.65:
        return "60-<65%"
    return ">=65%"


def _is_settled(record: DecisionRecord) -> bool:
    return (
        str(record.settlement_status or "").upper() == "SETTLED"
        and str(record.actual_result or "") in {"1", "X", "2"}
    )


def _probability_triplet(record: DecisionRecord) -> dict[str, float] | None:
    raw = record.raw_probabilities or {}
    values = {side: _as_float(raw.get(side)) for side in ("1", "X", "2")}
    if any(values[side] is None for side in values):
        return None
    if any(values[side] < 0.0 or values[side] > 1.0 for side in values):
        return None
    total = sum(values.values())
    if not 0.98 <= total <= 1.02 or total <= 0:
        return None
    # DecisionRecord stores rounded probabilities. Renormalization only removes
    # rounding drift after the strict 0.98-1.02 validity gate.
    return {side: values[side] / total for side in values}


def _probability_score(record: DecisionRecord) -> dict[str, float] | None:
    if not _is_settled(record):
        return None
    probabilities = _probability_triplet(record)
    actual = str(record.actual_result or "")
    if probabilities is None or actual not in probabilities:
        return None
    brier = sum(
        (probabilities[side] - (1.0 if side == actual else 0.0)) ** 2
        for side in ("1", "X", "2")
    )
    p_actual = min(max(probabilities[actual], PROBABILITY_EPSILON), 1.0)
    predicted = max(("1", "X", "2"), key=lambda side: probabilities[side])
    return {
        "brier": brier,
        "log_loss": -math.log(p_actual),
        "correct": 1.0 if predicted == actual else 0.0,
    }


def _favorite_calibration_observation(record: DecisionRecord) -> tuple[float, float] | None:
    if not _is_settled(record):
        return None
    confidence = _as_float(record.calibrated_favorite_confidence)
    selection = str(record.selection or "")
    actual = str(record.actual_result or "")
    if confidence is None or not 0.0 <= confidence <= 1.0:
        return None
    if selection not in {"1", "2"} or actual not in {"1", "X", "2"}:
        return None
    outcome = 1.0 if selection == actual else 0.0
    return confidence, outcome


def _calibration_summary(
    observations: Sequence[tuple[float, float]],
    *,
    include_curve: bool,
) -> dict[str, Any]:
    if not observations:
        return {
            "sample_size": 0,
            "average_confidence": None,
            "observed_success_rate": None,
            "calibration_gap": None,
            "calibration_gap_pp": None,
            "binary_brier": None,
            "ece": None,
            "ece_pp": None,
            "mce": None,
            "mce_pp": None,
            **({"curve": []} if include_curve else {}),
        }

    average_confidence = sum(conf for conf, _ in observations) / len(observations)
    observed_success = sum(outcome for _, outcome in observations) / len(observations)
    binary_brier = sum((conf - outcome) ** 2 for conf, outcome in observations) / len(observations)

    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for observation in observations:
        grouped[_confidence_bucket(observation[0])].append(observation)

    curve: list[dict[str, Any]] = []
    weighted_error = 0.0
    max_error = 0.0
    for bucket in CONFIDENCE_BUCKET_ORDER:
        rows = grouped.get(bucket) or []
        if not rows:
            continue
        avg_conf = sum(conf for conf, _ in rows) / len(rows)
        observed = sum(outcome for _, outcome in rows) / len(rows)
        gap = observed - avg_conf
        absolute_gap = abs(gap)
        weighted_error += (len(rows) / len(observations)) * absolute_gap
        max_error = max(max_error, absolute_gap)
        curve.append(
            {
                "bucket": bucket,
                "sample_size": len(rows),
                "average_confidence": _round(avg_conf),
                "observed_success_rate": _round(observed),
                "calibration_gap": _round(gap),
                "calibration_gap_pp": _round(gap * 100.0, 3),
            }
        )

    gap = observed_success - average_confidence
    result = {
        "sample_size": len(observations),
        "average_confidence": _round(average_confidence),
        "observed_success_rate": _round(observed_success),
        "calibration_gap": _round(gap),
        "calibration_gap_pp": _round(gap * 100.0, 3),
        "binary_brier": _round(binary_brier),
        "ece": _round(weighted_error),
        "ece_pp": _round(weighted_error * 100.0, 3),
        "mce": _round(max_error),
        "mce_pp": _round(max_error * 100.0, 3),
    }
    if include_curve:
        result["curve"] = curve
    return result


def _aggregate_pairs(pairs: Sequence[Pair], *, include_calibration_curve: bool) -> dict[str, Any]:
    total_records = len(pairs)
    settled_pairs = [(record, clv) for record, clv in pairs if _is_settled(record)]
    bet_pairs = [(record, clv) for record, clv in pairs if str(record.decision or "").upper() == "BET"]
    settled_bets = [
        (record, clv)
        for record, clv in settled_pairs
        if str(record.decision or "").upper() == "BET"
        and _as_float(record.hypothetical_pnl_units) is not None
    ]

    pnl_values = [_as_float(record.hypothetical_pnl_units) for record, _ in settled_bets]
    pnl_values = [value for value in pnl_values if value is not None]
    stake_units = float(len(pnl_values))
    pnl_units = sum(pnl_values)
    wins = sum(1 for record, _ in settled_bets if str(record.selection or "") == str(record.actual_result or ""))
    losses = len(pnl_values) - wins
    selected_odds = [
        value
        for record, _ in settled_bets
        if (value := _as_float(record.selected_odd)) is not None
    ]

    clv_bets = [(record, clv) for record, clv in bet_pairs if clv is not None]
    clv_odds_pct = [
        value
        for _, clv in clv_bets
        if clv is not None and (value := _as_float(clv.clv_odds_pct)) is not None
    ]
    clv_probability_pp = [
        value
        for _, clv in clv_bets
        if clv is not None and (value := _as_float(clv.clv_probability_pp)) is not None
    ]
    positive_clv = sum(
        1
        for _, clv in clv_bets
        if clv is not None
        and (value := _as_float(clv.clv_odds_decimal)) is not None
        and value > 0
    )

    scores = [score for record, _ in settled_pairs if (score := _probability_score(record)) is not None]
    calibration_observations = [
        observation
        for record, _ in settled_pairs
        if (observation := _favorite_calibration_observation(record)) is not None
    ]

    return {
        "sample": {
            "records": total_records,
            "settled_records": len(settled_pairs),
            "unsettled_records": total_records - len(settled_pairs),
            "bet_records": len(bet_pairs),
            "settled_bets": len(pnl_values),
            "probability_score_eligible": len(scores),
            "probability_score_excluded": len(settled_pairs) - len(scores),
            "calibration_eligible": len(calibration_observations),
            "clv_bet_records": len(clv_bets),
        },
        "economics": {
            "stake_units": _round(stake_units, 3),
            "pnl_units": _round(pnl_units, 6),
            "roi_decimal": _round(_safe_rate(pnl_units, stake_units)),
            "roi_pct": _round((_safe_rate(pnl_units, stake_units) * 100.0) if stake_units else None, 3),
            "wins": wins,
            "losses": losses,
            "win_rate": _round(_safe_rate(float(wins), float(len(pnl_values)))),
            "average_selected_odd": _round(sum(selected_odds) / len(selected_odds), 4) if selected_odds else None,
        },
        "clv": {
            "sample_size": len(clv_odds_pct),
            "bet_records": len(bet_pairs),
            "coverage_rate": _round(_safe_rate(float(len(clv_bets)), float(len(bet_pairs)))),
            "average_clv_odds_pct": _round(sum(clv_odds_pct) / len(clv_odds_pct), 3) if clv_odds_pct else None,
            "median_clv_odds_pct": _round(median(clv_odds_pct), 3) if clv_odds_pct else None,
            "positive_clv_count": positive_clv,
            "positive_clv_rate": _round(_safe_rate(float(positive_clv), float(len(clv_odds_pct)))),
            "average_clv_probability_pp": (
                _round(sum(clv_probability_pp) / len(clv_probability_pp), 3)
                if clv_probability_pp
                else None
            ),
        },
        "probability_quality": {
            "sample_size": len(scores),
            "brier_multiclass": _round(sum(score["brier"] for score in scores) / len(scores)) if scores else None,
            "log_loss": _round(sum(score["log_loss"] for score in scores) / len(scores)) if scores else None,
            "accuracy": _round(sum(score["correct"] for score in scores) / len(scores)) if scores else None,
        },
        "calibration": _calibration_summary(
            calibration_observations,
            include_curve=include_calibration_curve,
        ),
    }


def _breakdown(
    pairs: Sequence[Pair],
    key: Callable[[DecisionRecord], str],
    *,
    fixed_order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Pair]] = defaultdict(list)
    for pair in pairs:
        grouped[key(pair[0])].append(pair)

    if fixed_order is not None:
        labels = [label for label in fixed_order if label in grouped]
        labels.extend(sorted(label for label in grouped if label not in set(fixed_order)))
    else:
        labels = sorted(grouped, key=lambda value: value.casefold())

    return [
        {
            "group": label,
            **_aggregate_pairs(grouped[label], include_calibration_curve=False),
        }
        for label in labels
    ]


def _utc_date_bounds(start_date: date | None, end_date: date | None) -> tuple[datetime | None, datetime | None]:
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    start = None
    end_exclusive = None
    if start_date:
        start = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    if end_date:
        next_date = end_date + timedelta(days=1)
        end_exclusive = datetime.combine(next_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    return start, end_exclusive


def build_forward_test_report_v2(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    league: str | None = None,
    bookmaker: str | None = None,
    decision: str | None = None,
    source: str | None = None,
    max_records: int = DEFAULT_MAX_REPORT_RECORDS,
) -> dict[str, Any]:
    if start_date and end_date and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if max_records < 1 or max_records > MAX_REPORT_RECORDS:
        raise ValueError(f"max_records must be between 1 and {MAX_REPORT_RECORDS}")

    normalized_decision = decision.strip().upper() if decision else None
    if normalized_decision and normalized_decision not in {"BET", "NO_BET"}:
        raise ValueError("decision must be BET or NO_BET")

    ensure_forward_test_schema()
    start_utc, end_exclusive_utc = _utc_date_bounds(start_date, end_date)

    conditions = []
    if start_utc:
        conditions.append(DecisionRecord.fixture_starts_at >= start_utc)
    if end_exclusive_utc:
        conditions.append(DecisionRecord.fixture_starts_at < end_exclusive_utc)
    if league:
        conditions.append(DecisionRecord.league == league)
    if bookmaker:
        conditions.append(DecisionRecord.bookmaker == bookmaker)
    if normalized_decision:
        conditions.append(DecisionRecord.decision == normalized_decision)
    if source:
        conditions.append(DecisionRecord.source == source)

    count_query = select(func.count(DecisionRecord.id))
    query = (
        select(DecisionRecord, CLVRecord)
        .outerjoin(CLVRecord, CLVRecord.decision_record_id == DecisionRecord.id)
        .order_by(DecisionRecord.fixture_starts_at.asc(), DecisionRecord.id.asc())
    )
    for condition in conditions:
        count_query = count_query.where(condition)
        query = query.where(condition)

    with SessionLocal() as session:
        total_matching = int(session.scalar(count_query) or 0)
        if total_matching > max_records:
            raise ValueError(
                f"report scope has {total_matching} records; narrow filters or increase max_records "
                f"up to {MAX_REPORT_RECORDS} to avoid partial metrics"
            )
        rows = session.execute(query).all()

    pairs: list[Pair] = [(record, clv) for record, clv in rows]
    overview = _aggregate_pairs(pairs, include_calibration_curve=True)

    return {
        "status": "ok",
        "version": FORWARD_TEST_REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": BUSINESS_TIMEZONE,
        "filters": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "league": league,
            "bookmaker": bookmaker,
            "decision": normalized_decision,
            "source": source,
            "max_records": max_records,
        },
        "total_matching": total_matching,
        "overview": overview,
        "breakdowns": {
            "edge_bucket": _breakdown(
                pairs,
                lambda record: _edge_bucket(record.edge_percentage_points),
                fixed_order=EDGE_BUCKET_ORDER,
            ),
            "confidence_bucket": _breakdown(
                pairs,
                lambda record: _confidence_bucket(record.calibrated_favorite_confidence),
                fixed_order=CONFIDENCE_BUCKET_ORDER,
            ),
            "league": _breakdown(
                pairs,
                lambda record: str(record.league or "UNAVAILABLE"),
            ),
            "bookmaker": _breakdown(
                pairs,
                lambda record: str(record.bookmaker or "UNAVAILABLE"),
            ),
        },
        "definitions": {
            "roi": "sum settled BET hypothetical_pnl_units / one-unit stake per settled BET",
            "clv": "BET records only; decision_odd / closing_odd - 1 on exact bookmaker+market+selection",
            "brier_multiclass": "mean sum over 1/X/2 of (p_class - y_class)^2; lower is better",
            "log_loss": "mean -ln(raw probability assigned to actual 1/X/2 result); lower is better",
            "favorite_calibration": "calibrated team-favorite confidence versus whether selected favorite actually won",
            "ece": "confidence-bucket weighted absolute calibration gap",
            "mce": "maximum absolute confidence-bucket calibration gap",
            "edge_bucket_unit": "percentage points",
        },
        "policy": {
            "research_only": True,
            "real_money_execution_enabled": False,
            "decision_records_mutated": False,
            "clv_records_mutated": False,
            "roi_population": "SETTLED_BET_ONLY",
            "clv_population": "BET_WITH_FINALIZED_CLV",
            "probability_score_population": "ALL_SETTLED_RECORDS_WITH_VALID_RAW_1X2",
            "calibration_population": "ALL_SETTLED_RECORDS_WITH_VALID_FAVORITE_CONFIDENCE",
            "no_partial_metrics_when_scope_exceeds_max_records": True,
            "automatic_threshold_retuning_enabled": False,
            "report_is_diagnostic_not_policy_optimization": True,
        },
    }


@router.get("/report-v2")
def forward_test_report_v2_endpoint(
    start_date: date | None = None,
    end_date: date | None = None,
    league: str | None = Query(default=None, max_length=160),
    bookmaker: str | None = Query(default=None, max_length=120),
    decision: str | None = Query(default=None, max_length=12),
    source: str | None = Query(default=None, max_length=80),
    max_records: int = Query(default=DEFAULT_MAX_REPORT_RECORDS, ge=1, le=MAX_REPORT_RECORDS),
) -> dict[str, Any]:
    try:
        return build_forward_test_report_v2(
            start_date=start_date,
            end_date=end_date,
            league=league,
            bookmaker=bookmaker,
            decision=decision,
            source=source,
            max_records=max_records,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "version": FORWARD_TEST_REPORT_VERSION, "error": exc.__class__.__name__},
        ) from exc
