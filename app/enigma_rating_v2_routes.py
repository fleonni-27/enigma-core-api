from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.enigma_rating_v2 import build_enigma_rating_v2
from app.enigma_rating_v2_context import build_enigma_rating_v2_context

ENIGMA_RATING_V2_ROUTES_VERSION = "enigma_rating_v2_routes_v1"
router = APIRouter(prefix="/rating", tags=["Enigma Rating"])


@router.get("/context-v2/{sportmonks_fixture_id}")
def enigma_rating_v2_context_endpoint(sportmonks_fixture_id: int) -> dict[str, Any]:
    try:
        return build_enigma_rating_v2_context(int(sportmonks_fixture_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/enigma-v2/fixture/{sportmonks_fixture_id}")
def enigma_rating_v2_fixture_endpoint(
    sportmonks_fixture_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    selection = payload.get("selection")
    calibrated_probability = payload.get("calibrated_probability")
    if selection is None or calibrated_probability is None:
        raise HTTPException(
            status_code=400,
            detail="selection and calibrated_probability are required",
        )

    elo_home_advantage = float(payload.get("elo_home_advantage", 65.0))
    try:
        context = build_enigma_rating_v2_context(
            int(sportmonks_fixture_id),
            form_lookback=10,
            elo_home_advantage=elo_home_advantage,
        )
        if context.get("status") != "ok":
            return {
                "status": "not_ready",
                "version": ENIGMA_RATING_V2_ROUTES_VERSION,
                "context": context,
            }

        rating_args: dict[str, Any] = {
            **(context.get("rating_inputs") or {}),
            "selection": selection,
            "calibrated_probability": calibrated_probability,
        }
        for key in (
            "market_probability",
            "edge_percentage_points",
            "home_lineup_strength",
            "away_lineup_strength",
            "home_expected_xi_value",
            "home_absent_value",
            "away_expected_xi_value",
            "away_absent_value",
            "dixon_coles_rho",
            "elo_draw_parameter",
            "poisson_home_advantage_multiplier",
        ):
            if key in payload:
                rating_args[key] = payload[key]
        rating_args["elo_home_advantage"] = elo_home_advantage

        rating = build_enigma_rating_v2(**rating_args)
        return {
            "status": "ok",
            "version": ENIGMA_RATING_V2_ROUTES_VERSION,
            "fixture": context.get("fixture"),
            "rating": rating,
            "context_audit": {
                "history": context.get("history"),
                "elo": context.get("elo"),
                "lineup_context": context.get("lineup_context"),
                "policy": context.get("policy"),
            },
            "policy": {
                "research_only": True,
                "context_inputs_cannot_be_overridden_by_request": True,
                "lineup_impact_requires_explicit_auditable_values": True,
                "decision_engine_not_called": True,
                "prediction_not_persisted": True,
            },
        }
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
