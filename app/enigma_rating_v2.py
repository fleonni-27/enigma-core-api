from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.football_probability_models import (
    DEFAULT_DIXON_COLES_RHO,
    DEFAULT_ELO_DRAW_PARAMETER,
    DEFAULT_ELO_HOME_ADVANTAGE,
    derive_expected_goals,
    dixon_coles_1x2,
    elo_davidson_1x2,
    poisson_1x2,
)
from app.lineup_impact import quantify_lineup_impact, relative_lineup_support

ENIGMA_RATING_V2_VERSION = "enigma_rating_v2_research_v1"
router = APIRouter(prefix="/rating", tags=["Enigma Rating"])

COMPONENT_WEIGHTS_V2 = {
    "model_confidence": 15.0,
    "market_edge": 15.0,
    "poisson": 12.0,
    "dixon_coles": 12.0,
    "elo": 10.0,
    "xg_xga": 12.0,
    "recent_form_10": 10.0,
    "lineup_impact": 9.0,
    "home_advantage": 5.0,
}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _selection(value_home: float, value_away: float, selection: str, scale: float) -> float:
    delta = float(value_home) - float(value_away)
    if selection == "1":
        return _clip(0.5 + delta / (2.0 * scale))
    if selection == "2":
        return _clip(0.5 - delta / (2.0 * scale))
    return _clip(1.0 - abs(delta) / scale)


def _probability_support(probability: float) -> float:
    # 1/3 is neutral (50), 1.0 maps to 100 and 0.0 maps to 25.
    return _clip(0.5 + (float(probability) - (1.0 / 3.0)) / (4.0 / 3.0))


def _component(*, score: float | None, weight: float, source: str, detail: Any = None) -> dict[str, Any]:
    available = score is not None
    payload: dict[str, Any] = {
        "available": available,
        "score": round(float(score) * 100.0, 2) if available else None,
        "weight": float(weight),
        "source": source,
    }
    if detail is not None:
        payload["detail"] = detail
    return payload


def _rating_band(rating: float) -> str:
    if rating >= 80.0:
        return "ELITE"
    if rating >= 70.0:
        return "STRONG"
    if rating >= 60.0:
        return "POSITIVE"
    if rating >= 50.0:
        return "NEUTRAL"
    return "WEAK"


def build_enigma_rating_v2(
    *,
    selection: str,
    calibrated_probability: float,
    market_probability: float | None = None,
    edge_percentage_points: float | None = None,
    home_goals_for_avg: float | None = None,
    away_goals_for_avg: float | None = None,
    home_goals_against_avg: float | None = None,
    away_goals_against_avg: float | None = None,
    home_xg_for_avg: float | None = None,
    away_xg_for_avg: float | None = None,
    home_xg_against_avg: float | None = None,
    away_xg_against_avg: float | None = None,
    home_points_per_match_10: float | None = None,
    away_points_per_match_10: float | None = None,
    home_elo: float | None = None,
    away_elo: float | None = None,
    home_lineup_strength: float | None = None,
    away_lineup_strength: float | None = None,
    home_expected_xi_value: float | None = None,
    home_absent_value: float | None = None,
    away_expected_xi_value: float | None = None,
    away_absent_value: float | None = None,
    dixon_coles_rho: float = DEFAULT_DIXON_COLES_RHO,
    elo_home_advantage: float = DEFAULT_ELO_HOME_ADVANTAGE,
    elo_draw_parameter: float = DEFAULT_ELO_DRAW_PARAMETER,
    poisson_home_advantage_multiplier: float = 1.08,
) -> dict[str, Any]:
    selection = str(selection).strip().upper()
    if selection not in {"1", "X", "2"}:
        raise ValueError("selection must be 1, X, or 2")
    calibrated_probability = float(calibrated_probability)
    if calibrated_probability < 0.0 or calibrated_probability > 1.0:
        raise ValueError("calibrated_probability must be between 0 and 1")
    if market_probability is not None and not 0.0 < float(market_probability) < 1.0:
        raise ValueError("market_probability must be between 0 and 1")

    components: dict[str, dict[str, Any]] = {}
    signal_probabilities: dict[str, Any] = {}

    components["model_confidence"] = _component(
        score=_probability_support(calibrated_probability),
        weight=COMPONENT_WEIGHTS_V2["model_confidence"],
        source="calibrated Enigma selection probability",
        detail={"selection_probability": round(calibrated_probability, 8)},
    )

    edge_pp = edge_percentage_points
    if edge_pp is None and market_probability is not None:
        edge_pp = (calibrated_probability - float(market_probability)) * 100.0
    edge_score = None if edge_pp is None else _clip(0.5 + float(edge_pp) / 30.0)
    components["market_edge"] = _component(
        score=edge_score,
        weight=COMPONENT_WEIGHTS_V2["market_edge"],
        source="calibrated probability minus no-vig market probability",
        detail={"edge_percentage_points": round(float(edge_pp), 4)} if edge_pp is not None else None,
    )

    rate_model = derive_expected_goals(
        home_goals_for_avg=home_goals_for_avg,
        away_goals_for_avg=away_goals_for_avg,
        home_goals_against_avg=home_goals_against_avg,
        away_goals_against_avg=away_goals_against_avg,
        home_xg_for_avg=home_xg_for_avg,
        away_xg_for_avg=away_xg_for_avg,
        home_xg_against_avg=home_xg_against_avg,
        away_xg_against_avg=away_xg_against_avg,
        home_advantage_multiplier=poisson_home_advantage_multiplier,
    )
    if rate_model.get("status") == "ok":
        expected = rate_model["expected_goals"]
        poisson = poisson_1x2(expected["home"], expected["away"])
        dc = dixon_coles_1x2(expected["home"], expected["away"], rho=dixon_coles_rho)
        poisson_probability = float(poisson["probabilities"][selection])
        dc_probability = float(dc["probabilities"][selection])
        signal_probabilities["expected_goals"] = rate_model
        signal_probabilities["poisson"] = poisson
        signal_probabilities["dixon_coles"] = dc
        components["poisson"] = _component(
            score=_probability_support(poisson_probability),
            weight=COMPONENT_WEIGHTS_V2["poisson"],
            source="independent Poisson score grid",
            detail={"selection_probability": round(poisson_probability, 8), "input_quality": rate_model.get("input_quality")},
        )
        components["dixon_coles"] = _component(
            score=_probability_support(dc_probability),
            weight=COMPONENT_WEIGHTS_V2["dixon_coles"],
            source="Dixon-Coles low-score adjusted Poisson grid",
            detail={"selection_probability": round(dc_probability, 8), "rho": float(dixon_coles_rho)},
        )
    else:
        components["poisson"] = _component(score=None, weight=COMPONENT_WEIGHTS_V2["poisson"], source="independent Poisson score grid", detail=rate_model)
        components["dixon_coles"] = _component(score=None, weight=COMPONENT_WEIGHTS_V2["dixon_coles"], source="Dixon-Coles low-score adjusted Poisson grid", detail=rate_model)

    if home_elo is not None and away_elo is not None:
        elo = elo_davidson_1x2(
            home_elo,
            away_elo,
            home_advantage_elo=elo_home_advantage,
            draw_parameter=elo_draw_parameter,
        )
        elo_probability = float(elo["probabilities"][selection])
        signal_probabilities["elo"] = elo
        components["elo"] = _component(
            score=_probability_support(elo_probability),
            weight=COMPONENT_WEIGHTS_V2["elo"],
            source="Elo strengths with Davidson draw extension",
            detail={"selection_probability": round(elo_probability, 8)},
        )
    else:
        components["elo"] = _component(score=None, weight=COMPONENT_WEIGHTS_V2["elo"], source="Elo strengths with Davidson draw extension")

    xg_values = [home_xg_for_avg, away_xg_for_avg, home_xg_against_avg, away_xg_against_avg]
    if all(value is not None for value in xg_values):
        home_xg_strength = float(home_xg_for_avg) - float(home_xg_against_avg)
        away_xg_strength = float(away_xg_for_avg) - float(away_xg_against_avg)
        components["xg_xga"] = _component(
            score=_selection(home_xg_strength, away_xg_strength, selection, 3.0),
            weight=COMPONENT_WEIGHTS_V2["xg_xga"],
            source="rolling xG-for minus xG-against",
            detail={"home_net_xg": round(home_xg_strength, 4), "away_net_xg": round(away_xg_strength, 4)},
        )
    else:
        components["xg_xga"] = _component(score=None, weight=COMPONENT_WEIGHTS_V2["xg_xga"], source="rolling xG-for minus xG-against")

    if home_points_per_match_10 is not None and away_points_per_match_10 is not None:
        components["recent_form_10"] = _component(
            score=_selection(float(home_points_per_match_10), float(away_points_per_match_10), selection, 3.0),
            weight=COMPONENT_WEIGHTS_V2["recent_form_10"],
            source="10-match rolling points-per-match",
            detail={"home_ppm": float(home_points_per_match_10), "away_ppm": float(away_points_per_match_10)},
        )
    else:
        components["recent_form_10"] = _component(score=None, weight=COMPONENT_WEIGHTS_V2["recent_form_10"], source="10-match rolling points-per-match")

    lineup_detail: dict[str, Any] = {}
    if home_lineup_strength is None and home_expected_xi_value is not None and home_absent_value is not None:
        home_lineup = quantify_lineup_impact(expected_xi_value=home_expected_xi_value, absent_value=home_absent_value)
        home_lineup_strength = float(home_lineup["strength_retained"])
        lineup_detail["home"] = home_lineup
    if away_lineup_strength is None and away_expected_xi_value is not None and away_absent_value is not None:
        away_lineup = quantify_lineup_impact(expected_xi_value=away_expected_xi_value, absent_value=away_absent_value)
        away_lineup_strength = float(away_lineup["strength_retained"])
        lineup_detail["away"] = away_lineup
    if home_lineup_strength is not None and away_lineup_strength is not None:
        if not 0.0 <= float(home_lineup_strength) <= 1.0 or not 0.0 <= float(away_lineup_strength) <= 1.0:
            raise ValueError("lineup strengths must be between 0 and 1")
        components["lineup_impact"] = _component(
            score=relative_lineup_support(selection, float(home_lineup_strength), float(away_lineup_strength)),
            weight=COMPONENT_WEIGHTS_V2["lineup_impact"],
            source="auditable expected-XI value retained after confirmed absences",
            detail={
                "home_strength_retained": round(float(home_lineup_strength), 6),
                "away_strength_retained": round(float(away_lineup_strength), 6),
                **lineup_detail,
            },
        )
    else:
        components["lineup_impact"] = _component(score=None, weight=COMPONENT_WEIGHTS_V2["lineup_impact"], source="auditable expected-XI value retained after confirmed absences")

    home_score = 1.0 if selection == "1" else (0.5 if selection == "X" else 0.0)
    components["home_advantage"] = _component(
        score=home_score,
        weight=COMPONENT_WEIGHTS_V2["home_advantage"],
        source="explicit 1X2 home-field prior",
    )

    available = [component for component in components.values() if component["available"]]
    available_weight = sum(float(component["weight"]) for component in available)
    weighted = sum(float(component["score"]) * float(component["weight"]) for component in available)
    rating = weighted / available_weight if available_weight else 0.0
    total_weight = sum(COMPONENT_WEIGHTS_V2.values())
    coverage = available_weight / total_weight if total_weight else 0.0

    return {
        "status": "ok",
        "version": ENIGMA_RATING_V2_VERSION,
        "rating": round(rating, 2),
        "band": _rating_band(rating),
        "coverage_pct": round(coverage * 100.0, 2),
        "selection": selection,
        "components": components,
        "signal_probabilities": signal_probabilities,
        "missing_components": [name for name, component in components.items() if not component["available"]],
        "remaining_research_components_not_scored": [
            "competition_context",
            "head_to_head",
            "historical_brier_and_clv_similarity",
        ],
        "policy": {
            "research_only": True,
            "rating_is_not_a_probability": True,
            "rating_does_not_override_decision_engine": True,
            "production_model_version_unchanged": "baseline_1x2_temporal_v1",
            "standard_36_features_unchanged": True,
            "missing_components_are_not_imputed": True,
            "weights_are_renormalized_over_available_components": True,
            "poisson_dixon_coles_elo_are_research_signals": True,
            "lineup_player_values_must_be_auditable": True,
        },
    }


@router.post("/enigma-v2")
def enigma_rating_v2_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return build_enigma_rating_v2(**payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
