from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import median
from typing import Any, Callable, Sequence

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.database import SessionLocal
from app.forward_test_ledger import DecisionRecord, ensure_forward_test_schema
from app.forward_test_report_v2 import (
    BUSINESS_TIMEZONE,
    DEFAULT_MAX_REPORT_RECORDS,
    EDGE_BUCKET_ORDER,
    CONFIDENCE_BUCKET_ORDER,
    MAX_REPORT_RECORDS,
    _as_float,
    _calibration_summary,
    _confidence_bucket,
    _edge_bucket,
    _favorite_calibration_observation,
    _is_settled,
    _probability_triplet,
    _round,
    _safe_rate,
    _utc_date_bounds,
)
from app.odds_window_clv import CLVRecord

FORWARD_TEST_REPORT_V3_VERSION = "forward_test_report_v3"
UNIFORM_BRIER_1X2 = 2.0 / 3.0
UNIFORM_LOG_LOSS_1X2 = math.log(3.0)
DIRECTIONAL_SAMPLE_FLOOR = 30
DIRECTIONAL_CLV_COVERAGE_FLOOR = 0.80

PROBABILITY_BUCKET_ORDER = tuple(
    [f"{lower}-<{lower + 10}%" for lower in range(0, 90, 10)] + [">=90%"]
)

router = APIRouter(prefix="/research/forward-test", tags=["research"])
Pair = tuple[DecisionRecord, CLVRecord | None]


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _probability_bucket(value: float) -> str:
    probability = max(0.0, min(1.0, float(value)))
    if probability >= 0.90:
        return ">=90%"
    lower = int(probability * 10) * 10
    return f"{lower}-<{lower + 10}%"


def _probability_observation(record: DecisionRecord) -> dict[str, Any] | None:
    if not _is_settled(record):
        return None
    probabilities = _probability_triplet(record)
    actual = str(record.actual_result or "")
    if probabilities is None or actual not in probabilities:
        return None
    predicted = max(("1", "X", "2"), key=lambda side: probabilities[side])
    p_actual = min(max(probabilities[actual], 1e-12), 1.0)
    return {
        "probabilities": probabilities,
        "actual": actual,
        "predicted": predicted,
        "predicted_confidence": probabilities[predicted],
        "correct": 1.0 if predicted == actual else 0.0,
        "brier": sum(
            (probabilities[side] - (1.0 if side == actual else 0.0)) ** 2
            for side in ("1", "X", "2")
        ),
        "log_loss": -math.log(p_actual),
        "p_actual": probabilities[actual],
    }


def _reliability_summary(
    observations: Sequence[tuple[float, float]],
    *,
    include_curve: bool,
) -> dict[str, Any]:
    if not observations:
        return {
            "sample_size": 0,
            "average_probability": None,
            "observed_frequency": None,
            "calibration_gap": None,
            "calibration_gap_pp": None,
            "binary_brier": None,
            "ece": None,
            "ece_pp": None,
            "mce": None,
            "mce_pp": None,
            **({"curve": []} if include_curve else {}),
        }

    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for probability, outcome in observations:
        grouped[_probability_bucket(probability)].append((probability, outcome))

    weighted_error = 0.0
    max_error = 0.0
    curve: list[dict[str, Any]] = []
    total = len(observations)
    for bucket in PROBABILITY_BUCKET_ORDER:
        rows = grouped.get(bucket) or []
        if not rows:
            continue
        average_probability = sum(probability for probability, _ in rows) / len(rows)
        observed_frequency = sum(outcome for _, outcome in rows) / len(rows)
        gap = observed_frequency - average_probability
        absolute_gap = abs(gap)
        weighted_error += (len(rows) / total) * absolute_gap
        max_error = max(max_error, absolute_gap)
        curve.append(
            {
                "bucket": bucket,
                "sample_size": len(rows),
                "average_probability": _round(average_probability),
                "observed_frequency": _round(observed_frequency),
                "calibration_gap": _round(gap),
                "calibration_gap_pp": _round(gap * 100.0, 3),
            }
        )

    average_probability = sum(probability for probability, _ in observations) / total
    observed_frequency = sum(outcome for _, outcome in observations) / total
    binary_brier = sum((probability - outcome) ** 2 for probability, outcome in observations) / total
    gap = observed_frequency - average_probability
    result = {
        "sample_size": total,
        "average_probability": _round(average_probability),
        "observed_frequency": _round(observed_frequency),
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


def _probability_quality(records: Sequence[DecisionRecord]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    observations = [
        observation
        for record in records
        if (observation := _probability_observation(record)) is not None
    ]
    if not observations:
        return (
            {
                "sample_size": 0,
                "brier_multiclass": None,
                "log_loss": None,
                "accuracy": None,
                "average_probability_actual": None,
                "uniform_baseline": {
                    "brier_multiclass": _round(UNIFORM_BRIER_1X2),
                    "log_loss": _round(UNIFORM_LOG_LOSS_1X2),
                },
                "skill_vs_uniform": {
                    "brier_skill": None,
                    "log_loss_skill": None,
                },
                "empirical_climatology": {
                    "class_rates": {"1": None, "X": None, "2": None},
                    "brier_multiclass": None,
                    "log_loss": None,
                    "skill": {"brier_skill": None, "log_loss_skill": None},
                },
            },
            [],
        )

    sample_size = len(observations)
    brier = sum(item["brier"] for item in observations) / sample_size
    log_loss = sum(item["log_loss"] for item in observations) / sample_size
    accuracy = sum(item["correct"] for item in observations) / sample_size
    average_p_actual = sum(item["p_actual"] for item in observations) / sample_size

    class_rates = {
        side: sum(1 for item in observations if item["actual"] == side) / sample_size
        for side in ("1", "X", "2")
    }
    climatology_brier = 1.0 - sum(rate * rate for rate in class_rates.values())
    climatology_log_loss = -sum(
        rate * math.log(rate)
        for rate in class_rates.values()
        if rate > 0.0
    )

    result = {
        "sample_size": sample_size,
        "brier_multiclass": _round(brier),
        "log_loss": _round(log_loss),
        "accuracy": _round(accuracy),
        "average_probability_actual": _round(average_p_actual),
        "uniform_baseline": {
            "brier_multiclass": _round(UNIFORM_BRIER_1X2),
            "log_loss": _round(UNIFORM_LOG_LOSS_1X2),
        },
        "skill_vs_uniform": {
            "brier_skill": _round(1.0 - brier / UNIFORM_BRIER_1X2),
            "log_loss_skill": _round(1.0 - log_loss / UNIFORM_LOG_LOSS_1X2),
        },
        "empirical_climatology": {
            "class_rates": {side: _round(rate) for side, rate in class_rates.items()},
            "brier_multiclass": _round(climatology_brier),
            "log_loss": _round(climatology_log_loss),
            "skill": {
                "brier_skill": _round(1.0 - brier / climatology_brier) if climatology_brier > 0 else None,
                "log_loss_skill": _round(1.0 - log_loss / climatology_log_loss) if climatology_log_loss > 0 else None,
            },
        },
    }
    return result, observations


def _calibration_quality(
    settled_records: Sequence[DecisionRecord],
    probability_observations: Sequence[dict[str, Any]],
    *,
    include_curves: bool,
) -> dict[str, Any]:
    favorite_observations = [
        observation
        for record in settled_records
        if (observation := _favorite_calibration_observation(record)) is not None
    ]
    predicted_class = [
        (item["predicted_confidence"], item["correct"])
        for item in probability_observations
    ]
    classwise = []
    for side in ("1", "X", "2"):
        observations = [
            (item["probabilities"][side], 1.0 if item["actual"] == side else 0.0)
            for item in probability_observations
        ]
        classwise.append(
            {
                "class": side,
                **_reliability_summary(observations, include_curve=include_curves),
            }
        )

    return {
        "favorite_decision_confidence": _calibration_summary(
            favorite_observations,
            include_curve=include_curves,
        ),
        "predicted_class_raw_probability": _reliability_summary(
            predicted_class,
            include_curve=include_curves,
        ),
        "classwise_raw_probability": classwise,
    }


def _clv_quality(pairs: Sequence[Pair], *, as_of: datetime) -> dict[str, Any]:
    bet_pairs = [
        (record, clv)
        for record, clv in pairs
        if str(record.decision or "").upper() == "BET"
    ]
    due_pairs = [
        (record, clv)
        for record, clv in bet_pairs
        if record.fixture_starts_at is not None and record.fixture_starts_at <= as_of
    ]
    finalized_pairs = [(record, clv) for record, clv in due_pairs if clv is not None]

    odds_values = [
        value
        for _, clv in finalized_pairs
        if clv is not None and (value := _as_float(clv.clv_odds_pct)) is not None
    ]
    probability_values = [
        value
        for _, clv in finalized_pairs
        if clv is not None and (value := _as_float(clv.clv_probability_pp)) is not None
    ]
    lead_minutes = []
    post_kickoff_closing_count = 0
    for record, clv in finalized_pairs:
        if clv is None or clv.closing_quote_fetched_at is None or record.fixture_starts_at is None:
            continue
        lead = (record.fixture_starts_at - clv.closing_quote_fetched_at).total_seconds() / 60.0
        lead_minutes.append(lead)
        if lead < 0:
            post_kickoff_closing_count += 1

    positive_odds = sum(1 for value in odds_values if value > 0)
    positive_probability = sum(1 for value in probability_values if value > 0)

    def distribution(values: Sequence[float]) -> dict[str, Any]:
        return {
            "sample_size": len(values),
            "average": _round(sum(values) / len(values), 3) if values else None,
            "median": _round(median(values), 3) if values else None,
            "p25": _round(_quantile(values, 0.25), 3),
            "p75": _round(_quantile(values, 0.75), 3),
            "minimum": _round(min(values), 3) if values else None,
            "maximum": _round(max(values), 3) if values else None,
        }

    return {
        "bet_records": len(bet_pairs),
        "clv_due_bet_records": len(due_pairs),
        "finalized_clv_records": len(finalized_pairs),
        "missing_finalized_clv_records": len(due_pairs) - len(finalized_pairs),
        "finalized_coverage_rate": _round(
            _safe_rate(float(len(finalized_pairs)), float(len(due_pairs)))
        ),
        "odds_clv": {
            "coverage_rate": _round(_safe_rate(float(len(odds_values)), float(len(due_pairs)))),
            "positive_count": positive_odds,
            "positive_rate": _round(_safe_rate(float(positive_odds), float(len(odds_values)))),
            **distribution(odds_values),
        },
        "probability_clv": {
            "coverage_rate": _round(_safe_rate(float(len(probability_values)), float(len(due_pairs)))),
            "positive_count": positive_probability,
            "positive_rate": _round(_safe_rate(float(positive_probability), float(len(probability_values)))),
            **distribution(probability_values),
        },
        "closing_quote_lead_minutes": {
            **distribution(lead_minutes),
            "post_kickoff_count": post_kickoff_closing_count,
        },
    }


def _economics(pairs: Sequence[Pair]) -> dict[str, Any]:
    settled_bets = [
        record
        for record, _ in pairs
        if _is_settled(record)
        and str(record.decision or "").upper() == "BET"
        and _as_float(record.hypothetical_pnl_units) is not None
    ]
    pnl_values = [_as_float(record.hypothetical_pnl_units) for record in settled_bets]
    pnl_values = [value for value in pnl_values if value is not None]
    selected_odds = [
        value
        for record in settled_bets
        if (value := _as_float(record.selected_odd)) is not None
    ]
    wins = sum(1 for record in settled_bets if str(record.selection or "") == str(record.actual_result or ""))
    losses = len(settled_bets) - wins
    stake_units = float(len(pnl_values))
    pnl_units = sum(pnl_values)
    roi = _safe_rate(pnl_units, stake_units)
    return {
        "stake_units": _round(stake_units, 3),
        "pnl_units": _round(pnl_units, 6),
        "roi_decimal": _round(roi),
        "roi_pct": _round(roi * 100.0 if roi is not None else None, 3),
        "wins": wins,
        "losses": losses,
        "win_rate": _round(_safe_rate(float(wins), float(len(settled_bets)))),
        "average_selected_odd": _round(sum(selected_odds) / len(selected_odds), 4) if selected_odds else None,
    }


def _diagnostic_readiness(
    *,
    settled_records: int,
    settled_bets: int,
    probability_sample_size: int,
    calibration_sample_size: int,
    clv_due_bets: int,
    clv_coverage_rate: float | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if settled_records == 0:
        reasons.append("NO_SETTLED_RECORDS")
    elif settled_records < DIRECTIONAL_SAMPLE_FLOOR:
        reasons.append("EARLY_SETTLEMENT_SAMPLE")
    if settled_bets < DIRECTIONAL_SAMPLE_FLOOR:
        reasons.append("EARLY_ROI_SAMPLE")
    if probability_sample_size < DIRECTIONAL_SAMPLE_FLOOR:
        reasons.append("EARLY_PROBABILITY_SCORE_SAMPLE")
    if calibration_sample_size < DIRECTIONAL_SAMPLE_FLOOR:
        reasons.append("EARLY_CALIBRATION_SAMPLE")
    if clv_due_bets == 0:
        reasons.append("NO_CLV_DUE_BETS")
    elif clv_coverage_rate is None or clv_coverage_rate < DIRECTIONAL_CLV_COVERAGE_FLOOR:
        reasons.append("CLV_COVERAGE_INCOMPLETE")

    if "NO_SETTLED_RECORDS" in reasons:
        status = "DATA_NOT_READY"
    elif "CLV_COVERAGE_INCOMPLETE" in reasons:
        status = "PARTIAL_DATA"
    elif any(reason.startswith("EARLY_") for reason in reasons):
        status = "EARLY_SAMPLE"
    else:
        status = "READY_FOR_DIRECTIONAL_REVIEW"

    return {
        "status": status,
        "reason_codes": reasons,
        "directional_sample_floor": DIRECTIONAL_SAMPLE_FLOOR,
        "clv_coverage_floor": DIRECTIONAL_CLV_COVERAGE_FLOOR,
        "policy_note": "diagnostic observability gate only; not a statistical-significance threshold and not an auto-betting gate",
    }


def _aggregate_v3(
    pairs: Sequence[Pair],
    *,
    as_of: datetime,
    include_curves: bool,
) -> dict[str, Any]:
    records = [record for record, _ in pairs]
    settled_records = [record for record in records if _is_settled(record)]
    economics = _economics(pairs)
    probability_quality, probability_observations = _probability_quality(settled_records)
    calibration = _calibration_quality(
        settled_records,
        probability_observations,
        include_curves=include_curves,
    )
    clv = _clv_quality(pairs, as_of=as_of)

    readiness = _diagnostic_readiness(
        settled_records=len(settled_records),
        settled_bets=int(economics["stake_units"] or 0),
        probability_sample_size=int(probability_quality["sample_size"] or 0),
        calibration_sample_size=int(
            calibration["predicted_class_raw_probability"]["sample_size"] or 0
        ),
        clv_due_bets=int(clv["clv_due_bet_records"] or 0),
        clv_coverage_rate=_as_float(clv["finalized_coverage_rate"]),
    )

    return {
        "sample": {
            "records": len(records),
            "settled_records": len(settled_records),
            "unsettled_records": len(records) - len(settled_records),
            "bet_records": sum(1 for record in records if str(record.decision or "").upper() == "BET"),
            "no_bet_records": sum(1 for record in records if str(record.decision or "").upper() == "NO_BET"),
            "probability_score_eligible": probability_quality["sample_size"],
        },
        "economics": economics,
        "clv": clv,
        "probability_quality": probability_quality,
        "calibration": calibration,
        "diagnostic_readiness": readiness,
        "scorecard": {
            "status": readiness["status"],
            "roi_pct": economics["roi_pct"],
            "average_clv_odds_pct": clv["odds_clv"]["average"],
            "clv_coverage_rate": clv["finalized_coverage_rate"],
            "brier_multiclass": probability_quality["brier_multiclass"],
            "brier_skill_vs_uniform": probability_quality["skill_vs_uniform"]["brier_skill"],
            "log_loss": probability_quality["log_loss"],
            "log_loss_skill_vs_uniform": probability_quality["skill_vs_uniform"]["log_loss_skill"],
            "predicted_class_ece_pp": calibration["predicted_class_raw_probability"]["ece_pp"],
            "favorite_decision_ece_pp": calibration["favorite_decision_confidence"]["ece_pp"],
        },
    }


def _breakdown_v3(
    pairs: Sequence[Pair],
    key: Callable[[DecisionRecord], str],
    *,
    as_of: datetime,
    fixed_order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Pair]] = defaultdict(list)
    for pair in pairs:
        grouped[key(pair[0])].append(pair)

    if fixed_order is not None:
        fixed = set(fixed_order)
        labels = [label for label in fixed_order if label in grouped]
        labels.extend(sorted(label for label in grouped if label not in fixed))
    else:
        labels = sorted(grouped, key=lambda value: value.casefold())

    return [
        {
            "group": label,
            **_aggregate_v3(grouped[label], as_of=as_of, include_curves=False),
        }
        for label in labels
    ]


def build_forward_test_report_v3(
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
    generated_at = datetime.now(timezone.utc)
    overview = _aggregate_v3(pairs, as_of=generated_at, include_curves=True)

    return {
        "status": "ok",
        "version": FORWARD_TEST_REPORT_V3_VERSION,
        "generated_at": generated_at.isoformat(),
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
            "edge_bucket": _breakdown_v3(
                pairs,
                lambda record: _edge_bucket(record.edge_percentage_points),
                as_of=generated_at,
                fixed_order=EDGE_BUCKET_ORDER,
            ),
            "confidence_bucket": _breakdown_v3(
                pairs,
                lambda record: _confidence_bucket(record.calibrated_favorite_confidence),
                as_of=generated_at,
                fixed_order=CONFIDENCE_BUCKET_ORDER,
            ),
            "league": _breakdown_v3(
                pairs,
                lambda record: str(record.league or "UNAVAILABLE"),
                as_of=generated_at,
            ),
            "bookmaker": _breakdown_v3(
                pairs,
                lambda record: str(record.bookmaker or "UNAVAILABLE"),
                as_of=generated_at,
            ),
        },
        "definitions": {
            "roi": "sum settled BET hypothetical_pnl_units / one-unit stake per settled BET",
            "clv_odds": "decision_odd / closing_odd - 1 on exact bookmaker+market+selection; distribution reported in percent",
            "clv_probability": "closing no-vig probability minus decision no-vig probability; distribution reported in percentage points",
            "brier_multiclass": "mean sum over 1/X/2 of (p_class - y_class)^2; lower is better; unnormalized 0..2 scale preserved from V2",
            "log_loss": "mean -ln(raw probability assigned to actual 1/X/2 result); lower is better",
            "skill_score": "1 - observed_score / baseline_score; positive beats the baseline, negative underperforms it",
            "uniform_baseline": "fixed 1/X/2 baseline with probability 1/3 for each class",
            "empirical_climatology": "sample class-frequency baseline; diagnostic only and unstable for small samples",
            "predicted_class_calibration": "max raw 1/X/2 probability versus whether the argmax class was correct",
            "classwise_calibration": "each raw class probability versus observed frequency for that class using fixed 10pp bins",
            "favorite_decision_calibration": "calibrated favorite confidence versus whether the selected favorite actually won",
            "ece": "probability-bucket weighted absolute calibration gap",
            "mce": "maximum absolute probability-bucket calibration gap",
        },
        "policy": {
            "research_only": True,
            "real_money_execution_enabled": False,
            "decision_records_mutated": False,
            "clv_records_mutated": False,
            "automatic_threshold_retuning_enabled": False,
            "report_is_diagnostic_not_policy_optimization": True,
            "v2_endpoint_preserved": True,
            "diagnostic_readiness_is_not_statistical_significance": True,
            "no_partial_metrics_when_scope_exceeds_max_records": True,
        },
    }


@router.get("/report-v3")
def forward_test_report_v3_endpoint(
    start_date: date | None = None,
    end_date: date | None = None,
    league: str | None = Query(default=None, max_length=160),
    bookmaker: str | None = Query(default=None, max_length=120),
    decision: str | None = Query(default=None, max_length=12),
    source: str | None = Query(default=None, max_length=80),
    max_records: int = Query(default=DEFAULT_MAX_REPORT_RECORDS, ge=1, le=MAX_REPORT_RECORDS),
) -> dict[str, Any]:
    try:
        return build_forward_test_report_v3(
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
            detail={
                "status": "failed",
                "version": FORWARD_TEST_REPORT_V3_VERSION,
                "error": exc.__class__.__name__,
            },
        ) from exc
