from __future__ import annotations

from collections import defaultdict
from datetime import date
from itertools import product
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import app.enigma_rating_v2_evaluation as evaluation
import app.enigma_rating_v2_tuning as tuning
from app.baseline_1x2 import build_baseline_1x2_temporal_v1
from app.football_probability_models import dixon_coles_1x2, elo_davidson_1x2, elo_update

ENIGMA_RATING_V2_REFINEMENT_VERSION = "enigma_rating_v2_tuning_v1_refinement1"

REFINEMENT_ELO_K_GRID = (25.0, 30.0, 35.0, 40.0, 45.0)
REFINEMENT_ELO_HOME_ADVANTAGE_GRID = (80.0, 95.0, 110.0, 125.0, 140.0)
REFINEMENT_ELO_DRAW_PARAMETER_GRID = (0.30, 0.40, 0.50, 0.60, 0.70)
REFINEMENT_DIXON_COLES_RHO_GRID = (0.04, 0.08, 0.12, 0.16, 0.20, 0.24)

COARSE_ELO_WINNER = {
    "elo_k_factor": 30.0,
    "elo_home_advantage": 95.0,
    "elo_draw_parameter": 0.50,
}
COARSE_DIXON_COLES_WINNER = {"dixon_coles_rho": 0.12}

router = APIRouter(prefix="/research/enigma-rating-v2", tags=["research"])


def _elo_refinement_grid_search(
    groups: list[list[dict[str, Any]]],
    validation_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_by_fixture_id = {int(row["fixture_id"]): row for row in validation_targets}
    candidates: list[dict[str, Any]] = []

    for k_factor, home_advantage, draw_parameter in product(
        REFINEMENT_ELO_K_GRID,
        REFINEMENT_ELO_HOME_ADVANTAGE_GRID,
        REFINEMENT_ELO_DRAW_PARAMETER_GRID,
    ):
        ratings: dict[tuple[str, str], float] = {}
        matches: dict[tuple[str, str], int] = defaultdict(int)
        observations: list[dict[str, Any]] = []

        for group in groups:
            for event in group:
                target = target_by_fixture_id.get(int(event["fixture_id"]))
                if target is None:
                    continue
                home_key = (event["league_key"], event["home_team"])
                away_key = (event["league_key"], event["away_team"])
                if (
                    matches[home_key] < evaluation.MIN_ELO_TEAM_MATCHES
                    or matches[away_key] < evaluation.MIN_ELO_TEAM_MATCHES
                ):
                    continue
                probabilities = elo_davidson_1x2(
                    ratings.get(home_key, float(evaluation.DEFAULT_ELO_INITIAL)),
                    ratings.get(away_key, float(evaluation.DEFAULT_ELO_INITIAL)),
                    home_advantage_elo=float(home_advantage),
                    draw_parameter=float(draw_parameter),
                )["probabilities"]
                observations.append(
                    evaluation._probability_observation(probabilities, target["actual"])
                )

            for event in group:
                if event["home_score"] is None:
                    continue
                home_key = (event["league_key"], event["home_team"])
                away_key = (event["league_key"], event["away_team"])
                home_rating = ratings.get(home_key, float(evaluation.DEFAULT_ELO_INITIAL))
                away_rating = ratings.get(away_key, float(evaluation.DEFAULT_ELO_INITIAL))
                new_home, new_away = elo_update(
                    home_rating,
                    away_rating,
                    home_score=float(event["home_score"]),
                    k_factor=float(k_factor),
                    home_advantage_elo=float(home_advantage),
                )
                ratings[home_key] = new_home
                ratings[away_key] = new_away
                matches[home_key] += 1
                matches[away_key] += 1

        candidates.append(
            {
                "parameters": {
                    "elo_k_factor": float(k_factor),
                    "elo_home_advantage": float(home_advantage),
                    "elo_draw_parameter": float(draw_parameter),
                },
                "validation": tuning._score_summary(
                    observations, len(validation_targets)
                ),
            }
        )
    return candidates


def _dixon_coles_refinement_grid_search(
    dc_contexts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    targets = len(dc_contexts)
    for rho in REFINEMENT_DIXON_COLES_RHO_GRID:
        goals_observations: list[dict[str, Any]] = []
        xg_observations: list[dict[str, Any]] = []
        for context in dc_contexts:
            goals_expected = context.get("goals_expected")
            if goals_expected is not None:
                probabilities = dixon_coles_1x2(
                    float(goals_expected[0]), float(goals_expected[1]), rho=float(rho)
                )["probabilities"]
                goals_observations.append(
                    evaluation._probability_observation(probabilities, context["actual"])
                )
            xg_expected = context.get("xg_expected")
            if xg_expected is not None:
                probabilities = dixon_coles_1x2(
                    float(xg_expected[0]), float(xg_expected[1]), rho=float(rho)
                )["probabilities"]
                xg_observations.append(
                    evaluation._probability_observation(probabilities, context["actual"])
                )
        candidates.append(
            {
                "parameters": {"dixon_coles_rho": float(rho)},
                "validation_goals_only": tuning._score_summary(goals_observations, targets),
                "validation_xg_xga_secondary": tuning._score_summary(xg_observations, targets),
            }
        )
    return candidates


def build_enigma_rating_v2_tuning_refinement_v1(
    *,
    start_date: date = tuning.DEFAULT_TUNING_START_DATE,
    end_date: date = tuning.DEFAULT_TUNING_END_DATE,
    leagues: list[str] | None = None,
    max_rows: int = tuning.DEFAULT_MAX_ROWS,
    elo_warmup_days: int = tuning.DEFAULT_ELO_WARMUP_DAYS,
    poisson_home_multiplier: float = tuning.DEFAULT_POISSON_HOME_MULTIPLIER,
    include_grid: bool = False,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if end_date >= tuning.CONFIRMATION_HOLDOUT_START:
        raise ValueError(
            "validation refinement cannot use confirmation holdout dates on or after 2026-08-25"
        )
    if max_rows < 30 or max_rows > evaluation.MAX_EVALUATION_ROWS:
        raise ValueError(
            f"max_rows must be between 30 and {evaluation.MAX_EVALUATION_ROWS}"
        )
    selected_leagues = leagues or list(tuning.DEFAULT_TUNING_LEAGUES)

    baseline = build_baseline_1x2_temporal_v1(
        start_date=start_date,
        end_date=end_date,
        leagues=selected_leagues,
        family="STANDARD",
        lookback_matches=5,
        min_history_matches=3,
        train_ratio=0.70,
        validation_ratio=0.15,
        max_rows=max_rows,
        class_weight_balanced=False,
        include_predictions=True,
    )
    validation_targets = tuning._validation_targets_from_baseline(baseline)
    if not validation_targets:
        raise ValueError("validation partition has no eligible targets")

    material = tuning._build_validation_material(
        validation_targets,
        elo_warmup_days=int(elo_warmup_days),
        poisson_home_multiplier=float(poisson_home_multiplier),
    )
    elo_candidates = _elo_refinement_grid_search(material["groups"], validation_targets)
    dc_candidates = _dixon_coles_refinement_grid_search(material["dc_contexts"])
    elo_ranked = tuning._top_candidates(elo_candidates, "validation", limit=10)
    dc_ranked = tuning._top_candidates(dc_candidates, "validation_goals_only", limit=10)

    response: dict[str, Any] = {
        "status": "ok",
        "version": ENIGMA_RATING_V2_REFINEMENT_VERSION,
        "research_only": True,
        "selection_window": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "leagues": selected_leagues,
            "partition": "validation",
        },
        "baseline": {
            "baseline_id": baseline.get("baseline_id"),
            "baseline_sha256": baseline.get("baseline_sha256"),
            "split_sha256": (baseline.get("parent") or {}).get("split_sha256"),
            "family": baseline.get("family"),
            "feature_count": (baseline.get("training") or {}).get("feature_count"),
        },
        "objective": {
            "primary": "validation_brier_multiclass",
            "tie_break": "validation_log_loss",
            "lower_is_better": True,
            "test_partition_used_for_selection": False,
        },
        "coarse_winner_reference": {
            "elo": COARSE_ELO_WINNER,
            "dixon_coles": COARSE_DIXON_COLES_WINNER,
        },
        "elo": {
            "grid": {
                "k_factor": list(REFINEMENT_ELO_K_GRID),
                "home_advantage": list(REFINEMENT_ELO_HOME_ADVANTAGE_GRID),
                "draw_parameter": list(REFINEMENT_ELO_DRAW_PARAMETER_GRID),
                "candidate_count": len(elo_candidates),
            },
            "winner": elo_ranked[0],
            "top_candidates": elo_ranked,
        },
        "dixon_coles": {
            "grid": {
                "rho": list(REFINEMENT_DIXON_COLES_RHO_GRID),
                "candidate_count": len(dc_candidates),
            },
            "selection_basis": "goals-only validation arm; xG/xGA remains secondary diagnostic",
            "winner": dc_ranked[0],
            "top_candidates": dc_ranked,
        },
        "confirmation_holdout": {
            "status": "RESERVED_UNTOUCHED",
            "start_date": tuning.CONFIRMATION_HOLDOUT_START.isoformat(),
            "minimum_eligible_targets_before_confirmation": tuning.CONFIRMATION_MIN_TARGETS,
            "confirmation_must_use_frozen_parameters": True,
        },
        "audit": material["audit"],
        "policy": {
            "validation_only_refinement": True,
            "one_refinement_stage_only": True,
            "no_further_parameter_search_before_confirmation": True,
            "existing_observed_test_is_not_used_for_selection": True,
            "future_confirmation_holdout_untouched": True,
            "same_timestamp_leakage_guard_preserved": True,
            "elo_1460_day_warmup_preserved": int(elo_warmup_days) == 1460,
            "production_standard_model_unchanged": True,
            "decision_engine_unchanged": True,
            "prediction_and_ledger_persistence_unchanged": True,
        },
    }
    if include_grid:
        response["elo"]["all_candidates"] = elo_candidates
        response["dixon_coles"]["all_candidates"] = dc_candidates
    return response


@router.get("/tuning-v1-refinement1")
def enigma_rating_v2_tuning_refinement_endpoint(
    start_date: date = Query(default=tuning.DEFAULT_TUNING_START_DATE),
    end_date: date = Query(default=tuning.DEFAULT_TUNING_END_DATE),
    leagues: list[str] | None = Query(default=None),
    max_rows: int = Query(default=tuning.DEFAULT_MAX_ROWS, ge=30, le=evaluation.MAX_EVALUATION_ROWS),
    elo_warmup_days: int = Query(default=tuning.DEFAULT_ELO_WARMUP_DAYS, ge=180, le=3650),
    poisson_home_multiplier: float = Query(default=tuning.DEFAULT_POISSON_HOME_MULTIPLIER, ge=0.8, le=1.3),
    include_grid: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return build_enigma_rating_v2_tuning_refinement_v1(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            max_rows=max_rows,
            elo_warmup_days=elo_warmup_days,
            poisson_home_multiplier=poisson_home_multiplier,
            include_grid=include_grid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
