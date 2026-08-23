from __future__ import annotations

import math
import unicodedata
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Fixture, OddsSnapshot, Prediction

DECISION_ENGINE_VERSION = "decision_engine_v1"
CALIBRATION_VERSION = "favorite_confidence_calibration_v1"
CALIBRATION_ID = "favorite_confidence_calibration_v1:f92a13d043797561"
PLATT_COEFFICIENT = 0.34169
PLATT_INTERCEPT = -0.214051
SUPPORTED_BASE_MODEL_PREFIX = "baseline_1x2_temporal_v1"

DEFAULT_MIN_EDGE = 0.05
DEFAULT_MIN_EXPECTED_VALUE = 0.03
DEFAULT_MIN_CALIBRATED_CONFIDENCE = 0.45
DEFAULT_MAX_OVERROUND = 0.12
DEFAULT_MAX_QUOTE_SPAN_SECONDS = 300


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None else None


def _normalized_text(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return " ".join(ascii_text.lower().strip().split())


def _selection_side(selection: str, home_team: str, away_team: str) -> str | None:
    value = _normalized_text(selection)
    home = _normalized_text(home_team)
    away = _normalized_text(away_team)

    if value in {"1", "home", "home team", "mandante"} or value == home:
        return "1"
    if value in {"x", "draw", "tie", "empate"}:
        return "X"
    if value in {"2", "away", "away team", "visitante"} or value == away:
        return "2"
    return None


def _is_1x2_market(market: str | None) -> bool:
    name = _normalized_text(market)
    blocked = (
        "double chance",
        "draw no bet",
        "dnb",
        "half time",
        "halftime",
        "1st half",
        "first half",
        "2nd half",
        "second half",
        "correct score",
    )
    if any(token in name for token in blocked):
        return False
    accepted = (
        "1x2",
        "fulltime result",
        "full time result",
        "match winner",
        "3-way result",
        "3 way result",
    )
    return any(token in name for token in accepted)


def _logit(probability: float) -> float:
    eps = 1e-6
    p = min(max(float(probability), eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def calibrated_favorite_confidence(raw_favorite_probability: float) -> float:
    return _sigmoid(PLATT_INTERCEPT + PLATT_COEFFICIENT * _logit(raw_favorite_probability))


def _validate_probabilities(p_home: float, p_draw: float, p_away: float) -> tuple[bool, str | None]:
    probabilities = [p_home, p_draw, p_away]
    if any(not math.isfinite(p) or p < 0.0 or p > 1.0 for p in probabilities):
        return False, "PROBABILITY_OUT_OF_RANGE"
    total = sum(probabilities)
    if not 0.98 <= total <= 1.02:
        return False, "PROBABILITIES_DO_NOT_SUM_TO_ONE"
    return True, None


def evaluate_1x2_quote(
    *,
    p_home: float,
    p_draw: float,
    p_away: float,
    odd_home: float,
    odd_draw: float,
    odd_away: float,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_expected_value: float = DEFAULT_MIN_EXPECTED_VALUE,
    min_calibrated_confidence: float = DEFAULT_MIN_CALIBRATED_CONFIDENCE,
    max_overround: float = DEFAULT_MAX_OVERROUND,
    require_team_favorite_top_class: bool = True,
) -> dict[str, Any]:
    valid, error = _validate_probabilities(p_home, p_draw, p_away)
    if not valid:
        return {
            "status": "invalid_input",
            "decision": "NO_BET",
            "reason_codes": [error],
            "execution_mode": "RESEARCH_ONLY",
        }

    odds = {"1": float(odd_home), "X": float(odd_draw), "2": float(odd_away)}
    if any(not math.isfinite(odd) or odd <= 1.0 for odd in odds.values()):
        return {
            "status": "invalid_input",
            "decision": "NO_BET",
            "reason_codes": ["INVALID_ODDS"],
            "execution_mode": "RESEARCH_ONLY",
        }

    raw = {"1": float(p_home), "X": float(p_draw), "2": float(p_away)}
    team_favorite = "1" if raw["1"] >= raw["2"] else "2"
    top_class = max(raw, key=raw.get)
    raw_favorite_probability = raw[team_favorite]
    calibrated_confidence = calibrated_favorite_confidence(raw_favorite_probability)

    implied = {side: 1.0 / odd for side, odd in odds.items()}
    implied_sum = sum(implied.values())
    overround = implied_sum - 1.0
    no_vig = {side: probability / implied_sum for side, probability in implied.items()}

    selected_odd = odds[team_favorite]
    selected_market_probability = no_vig[team_favorite]
    edge = calibrated_confidence - selected_market_probability
    expected_value = calibrated_confidence * selected_odd - 1.0

    reason_codes: list[str] = []
    if require_team_favorite_top_class and top_class != team_favorite:
        reason_codes.append("DRAW_IS_TOP_MODEL_CLASS")
    if overround > max_overround:
        reason_codes.append("MARKET_OVERROUND_TOO_HIGH")
    if calibrated_confidence < min_calibrated_confidence:
        reason_codes.append("CALIBRATED_CONFIDENCE_BELOW_MINIMUM")
    if edge < min_edge:
        reason_codes.append("EDGE_BELOW_MINIMUM")
    if expected_value < min_expected_value:
        reason_codes.append("EXPECTED_VALUE_BELOW_MINIMUM")

    decision = "BET" if not reason_codes else "NO_BET"
    return {
        "status": "ok",
        "version": DECISION_ENGINE_VERSION,
        "decision": decision,
        "selection": team_favorite,
        "execution_mode": "RESEARCH_ONLY",
        "auto_execution": False,
        "reason_codes": reason_codes,
        "model": {
            "raw_probabilities": {side: _round(value) for side, value in raw.items()},
            "top_class": top_class,
            "team_favorite": team_favorite,
            "raw_favorite_probability": _round(raw_favorite_probability),
        },
        "calibration": {
            "version": CALIBRATION_VERSION,
            "calibration_id": CALIBRATION_ID,
            "method": "platt_scaling_binary",
            "coefficient": PLATT_COEFFICIENT,
            "intercept": PLATT_INTERCEPT,
            "calibrated_favorite_confidence": _round(calibrated_confidence),
            "raw_1x2_probabilities_rewritten": False,
        },
        "market": {
            "odds": {side: _round(value, 4) for side, value in odds.items()},
            "raw_implied_probabilities": {side: _round(value) for side, value in implied.items()},
            "no_vig_probabilities": {side: _round(value) for side, value in no_vig.items()},
            "overround": _round(overround),
            "selected_odd": _round(selected_odd, 4),
            "selected_no_vig_probability": _round(selected_market_probability),
        },
        "value": {
            "edge_probability_points": _round(edge),
            "edge_percentage_points": _round(edge * 100.0, 3),
            "expected_value_decimal": _round(expected_value),
            "expected_value_pct": _round(expected_value * 100.0, 3),
        },
        "thresholds": {
            "min_edge": min_edge,
            "min_expected_value": min_expected_value,
            "min_calibrated_confidence": min_calibrated_confidence,
            "max_overround": max_overround,
            "require_team_favorite_top_class": require_team_favorite_top_class,
            "optimized_on_test_set": False,
        },
        "policy": {
            "raw_probability_used_for_ranking": True,
            "calibrated_probability_used_for_value": True,
            "bookmaker_margin_removed_for_edge": True,
            "bet_requires_positive_policy_gates": True,
            "stake_sizing_enabled": False,
            "real_money_execution_enabled": False,
        },
    }


def _quote_span_seconds(rows: list[OddsSnapshot]) -> float:
    timestamps = [row.fetched_at for row in rows if row.fetched_at is not None]
    if len(timestamps) < 2:
        return 0.0
    return max(0.0, (max(timestamps) - min(timestamps)).total_seconds())


def _prediction_payload(prediction: Prediction) -> dict[str, Any]:
    return {
        "prediction_id": prediction.id,
        "prediction_window": prediction.prediction_window,
        "model_version": prediction.model_version,
        "generated_at": prediction.generated_at.isoformat() if prediction.generated_at else None,
        "p_home": _round(float(prediction.p_home)),
        "p_draw": _round(float(prediction.p_draw)),
        "p_away": _round(float(prediction.p_away)),
    }


def evaluate_fixture_decision(
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
    include_market_candidates: bool = False,
) -> dict[str, Any]:
    with SessionLocal() as session:
        fixture = session.scalar(select(Fixture).where(Fixture.sportmonks_id == sportmonks_fixture_id))
        if fixture is None:
            return {
                "status": "fixture_not_found",
                "version": DECISION_ENGINE_VERSION,
                "sportmonks_fixture_id": sportmonks_fixture_id,
                "decision": "NO_BET",
                "reason_codes": ["FIXTURE_NOT_FOUND"],
            }

        prediction_query = select(Prediction).where(Prediction.fixture_id == fixture.id)
        if prediction_window:
            prediction_query = prediction_query.where(Prediction.prediction_window == prediction_window)
        if model_version:
            prediction_query = prediction_query.where(Prediction.model_version == model_version)
        prediction_query = prediction_query.order_by(Prediction.generated_at.desc(), Prediction.id.desc())
        prediction = session.scalar(prediction_query.limit(1))

        fixture_payload = {
            "fixture_id": fixture.id,
            "sportmonks_fixture_id": fixture.sportmonks_id,
            "league": fixture.league_name,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
            "status": fixture.status,
        }

        if prediction is None:
            return {
                "status": "not_ready",
                "version": DECISION_ENGINE_VERSION,
                "decision": "NO_BET",
                "reason_codes": ["PREDICTION_NOT_AVAILABLE"],
                "fixture": fixture_payload,
            }

        odds_query = select(OddsSnapshot).where(OddsSnapshot.fixture_id == fixture.id)
        if snapshot_window:
            odds_query = odds_query.where(OddsSnapshot.snapshot_window == snapshot_window)
        odds_rows = session.scalars(odds_query.order_by(OddsSnapshot.fetched_at.desc(), OddsSnapshot.id.desc())).all()

    prediction_data = _prediction_payload(prediction)
    model_compatibility = (
        "SUPPORTED_BASELINE"
        if str(prediction.model_version or "").startswith(SUPPORTED_BASE_MODEL_PREFIX)
        else "UNVERIFIED_MODEL_VERSION"
    )

    groups: dict[tuple[str, str, str | None], dict[str, OddsSnapshot]] = {}
    for row in odds_rows:
        if not _is_1x2_market(row.market):
            continue
        side = _selection_side(row.selection, fixture.home_team, fixture.away_team)
        if side is None:
            continue
        key = (row.bookmaker, row.market, row.snapshot_window)
        side_rows = groups.setdefault(key, {})
        current = side_rows.get(side)
        if current is None or (row.fetched_at, row.id) > (current.fetched_at, current.id):
            side_rows[side] = row

    candidates: list[dict[str, Any]] = []
    rejected_groups: list[dict[str, Any]] = []
    for (bookmaker, market, window), by_side in groups.items():
        missing = [side for side in ("1", "X", "2") if side not in by_side]
        if missing:
            rejected_groups.append(
                {
                    "bookmaker": bookmaker,
                    "market": market,
                    "snapshot_window": window,
                    "reason": "INCOMPLETE_1X2_MARKET",
                    "missing_selections": missing,
                }
            )
            continue

        quote_rows = [by_side["1"], by_side["X"], by_side["2"]]
        quote_span = _quote_span_seconds(quote_rows)
        if quote_span > max_quote_span_seconds:
            rejected_groups.append(
                {
                    "bookmaker": bookmaker,
                    "market": market,
                    "snapshot_window": window,
                    "reason": "QUOTE_TIMESTAMPS_TOO_FAR_APART",
                    "quote_span_seconds": _round(quote_span, 3),
                }
            )
            continue

        evaluation = evaluate_1x2_quote(
            p_home=float(prediction.p_home),
            p_draw=float(prediction.p_draw),
            p_away=float(prediction.p_away),
            odd_home=float(by_side["1"].odd),
            odd_draw=float(by_side["X"].odd),
            odd_away=float(by_side["2"].odd),
            min_edge=min_edge,
            min_expected_value=min_expected_value,
            min_calibrated_confidence=min_calibrated_confidence,
            max_overround=max_overround,
            require_team_favorite_top_class=require_team_favorite_top_class,
        )
        evaluation["bookmaker"] = bookmaker
        evaluation["market_name"] = market
        evaluation["snapshot_window"] = window
        evaluation["quote_span_seconds"] = _round(quote_span, 3)
        fetched_times = [row.fetched_at for row in quote_rows if row.fetched_at is not None]
        evaluation["latest_quote_fetched_at"] = max(fetched_times).isoformat() if fetched_times else None
        candidates.append(evaluation)

    base = {
        "version": DECISION_ENGINE_VERSION,
        "fixture": fixture_payload,
        "prediction": prediction_data,
        "model_compatibility": model_compatibility,
        "calibration": {
            "version": CALIBRATION_VERSION,
            "calibration_id": CALIBRATION_ID,
            "coefficient": PLATT_COEFFICIENT,
            "intercept": PLATT_INTERCEPT,
            "promotion_status": "PROMOTED_AFTER_FINAL_OOS_GATE",
        },
        "market_scan": {
            "odds_rows_considered": len(odds_rows),
            "grouped_1x2_markets": len(groups),
            "complete_candidate_markets": len(candidates),
            "rejected_market_groups": len(rejected_groups),
            "max_quote_span_seconds": max_quote_span_seconds,
        },
        "execution_mode": "RESEARCH_ONLY",
        "auto_execution": False,
    }

    if model_compatibility != "SUPPORTED_BASELINE":
        return {
            "status": "not_ready",
            **base,
            "decision": "NO_BET",
            "reason_codes": ["UNVERIFIED_MODEL_VERSION_FOR_CALIBRATOR"],
        }

    if not candidates:
        return {
            "status": "not_ready",
            **base,
            "decision": "NO_BET",
            "reason_codes": ["COMPLETE_1X2_ODDS_NOT_AVAILABLE"],
            "rejected_market_groups": rejected_groups[:20] if include_market_candidates else None,
        }

    candidates.sort(
        key=lambda item: (
            float((item.get("value") or {}).get("expected_value_decimal") or -999.0),
            float((item.get("value") or {}).get("edge_probability_points") or -999.0),
        ),
        reverse=True,
    )
    best = candidates[0]
    response = {
        "status": "ok",
        **base,
        "decision": best["decision"],
        "selection": best.get("selection"),
        "reason_codes": best.get("reason_codes") or [],
        "best_market": {
            "bookmaker": best["bookmaker"],
            "market_name": best["market_name"],
            "snapshot_window": best["snapshot_window"],
            "quote_span_seconds": best["quote_span_seconds"],
            "latest_quote_fetched_at": best["latest_quote_fetched_at"],
            "market": best["market"],
            "value": best["value"],
            "thresholds": best["thresholds"],
        },
        "decision_model": best["model"],
        "decision_calibration": best["calibration"],
        "policy": {
            **best["policy"],
            "best_complete_bookmaker_market_selected_by_expected_value": True,
            "decision_thresholds_are_initial_not_test_optimized": True,
        },
    }
    if include_market_candidates:
        response["market_candidates"] = candidates[:20]
        response["rejected_market_groups"] = rejected_groups[:20]
    return response
