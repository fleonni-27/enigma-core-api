from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

ENIGMA_RATING_VERSION = "enigma_rating_v1"
router = APIRouter(prefix="/rating", tags=["Enigma Rating"])

# V1 is deliberately transparent. It scores only evidence already available in
# the production Enigma Core; unavailable future modules are reported instead
# of being silently approximated.
COMPONENT_WEIGHTS = {
    "recent_form": 25.0,
    "offensive_defensive_efficiency": 20.0,
    "home_advantage": 10.0,
    "model_confidence": 20.0,
    "market_edge": 25.0,
}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _normalize_delta(value: float | None, scale: float) -> float | None:
    if value is None:
        return None
    return _clip(0.5 + (float(value) / (2.0 * scale)))


def build_enigma_rating_v1(
    *,
    selection: str,
    calibrated_probability: float,
    market_probability: float | None,
    edge_percentage_points: float | None,
    home_points_per_match: float | None = None,
    away_points_per_match: float | None = None,
    home_goals_for_avg: float | None = None,
    away_goals_for_avg: float | None = None,
    home_goals_against_avg: float | None = None,
    away_goals_against_avg: float | None = None,
) -> dict[str, Any]:
    selection = str(selection).strip().upper()
    if selection not in {"1", "X", "2"}:
        raise ValueError("selection must be 1, X, or 2")

    components: dict[str, dict[str, Any]] = {}

    form_delta = None
    if home_points_per_match is not None and away_points_per_match is not None:
        raw_delta = float(home_points_per_match) - float(away_points_per_match)
        if selection == "2":
            raw_delta *= -1.0
        if selection == "X":
            form_score = _clip(1.0 - abs(raw_delta) / 3.0)
        else:
            form_score = _normalize_delta(raw_delta, 3.0)
        form_delta = raw_delta
        components["recent_form"] = {"available": True, "score": round(form_score * 100, 2), "weight": COMPONENT_WEIGHTS["recent_form"], "source": "5-match rolling points-per-match"}
    else:
        components["recent_form"] = {"available": False, "score": None, "weight": COMPONENT_WEIGHTS["recent_form"], "source": "5-match rolling points-per-match"}

    efficiency_score = None
    required = [home_goals_for_avg, away_goals_for_avg, home_goals_against_avg, away_goals_against_avg]
    if all(value is not None for value in required):
        home_strength = float(home_goals_for_avg) - float(home_goals_against_avg)
        away_strength = float(away_goals_for_avg) - float(away_goals_against_avg)
        delta = home_strength - away_strength
        if selection == "2":
            delta *= -1.0
        if selection == "X":
            efficiency_score = _clip(1.0 - abs(delta) / 4.0)
        else:
            efficiency_score = _normalize_delta(delta, 4.0)
        components["offensive_defensive_efficiency"] = {"available": True, "score": round(efficiency_score * 100, 2), "weight": COMPONENT_WEIGHTS["offensive_defensive_efficiency"], "source": "rolling goals-for minus goals-against"}
    else:
        components["offensive_defensive_efficiency"] = {"available": False, "score": None, "weight": COMPONENT_WEIGHTS["offensive_defensive_efficiency"], "source": "rolling goals-for minus goals-against"}

    home_score = 1.0 if selection == "1" else (0.5 if selection == "X" else 0.0)
    components["home_advantage"] = {"available": True, "score": round(home_score * 100, 2), "weight": COMPONENT_WEIGHTS["home_advantage"], "source": "1X2 selection/home-field prior"}

    confidence_score = _clip((float(calibrated_probability) - (1.0 / 3.0)) / (2.0 / 3.0))
    components["model_confidence"] = {"available": True, "score": round(confidence_score * 100, 2), "weight": COMPONENT_WEIGHTS["model_confidence"], "source": "calibrated Enigma probability"}

    edge_score = None
    if edge_percentage_points is not None:
        edge_score = _clip(float(edge_percentage_points) / 15.0)
    elif market_probability is not None:
        edge_score = _clip(((float(calibrated_probability) - float(market_probability)) * 100.0) / 15.0)
    components["market_edge"] = {"available": edge_score is not None, "score": round(edge_score * 100, 2) if edge_score is not None else None, "weight": COMPONENT_WEIGHTS["market_edge"], "source": "Enigma probability vs implied market probability"}

    available = [item for item in components.values() if item["available"]]
    available_weight = sum(float(item["weight"]) for item in available)
    weighted = sum(float(item["score"]) * float(item["weight"]) for item in available)
    rating = weighted / available_weight if available_weight else 0.0
    coverage = available_weight / sum(COMPONENT_WEIGHTS.values())

    if rating >= 80:
        band = "ELITE"
    elif rating >= 70:
        band = "STRONG"
    elif rating >= 60:
        band = "POSITIVE"
    elif rating >= 50:
        band = "NEUTRAL"
    else:
        band = "WEAK"

    return {
        "status": "ok",
        "version": ENIGMA_RATING_VERSION,
        "rating": round(rating, 2),
        "band": band,
        "coverage_pct": round(coverage * 100.0, 2),
        "selection": selection,
        "components": components,
        "future_components_not_scored": [
            "poisson",
            "dixon_coles",
            "elo",
            "full_xg_xga",
            "10_match_form",
            "lineup_absence_impact",
            "competition_context",
            "head_to_head",
            "historical_brier_and_clv_similarity",
        ],
        "policy": {
            "research_only": True,
            "transparent_weighted_score": True,
            "missing_components_are_not_imputed": True,
            "weights_are_renormalized_over_available_components": True,
            "rating_is_not_a_probability": True,
            "rating_does_not_override_decision_engine": True,
        },
    }


@router.post("/enigma")
def enigma_rating_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return build_enigma_rating_v1(**payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
