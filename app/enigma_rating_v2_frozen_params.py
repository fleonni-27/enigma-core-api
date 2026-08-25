from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import APIRouter

FROZEN_TUNING_VERSION = "enigma_rating_v2_frozen_tuning_v1"
FROZEN_SELECTION_SHA256 = "3d7a37a3c81cf383f08057e6ecfa1b8cf18abe5a2a7421698fdcf31acb736dcc"

SELECTION_START_DATE = "2026-01-01"
SELECTION_END_DATE = "2026-08-24"
SELECTION_PARTITION = "validation"
SELECTION_LEAGUES = ("Serie A", "Serie B", "Copa Libertadores", "La Liga")

BASELINE_ID = "baseline_1x2_temporal_v1:7331869f9d6795d1"
BASELINE_SHA256 = "7331869f9d6795d1981842644c8e4fb9664fe8cbac869dfaf71ded98847ca701"
SPLIT_SHA256 = "e08ed238afb321a9b7da302a94d72ad330f521208143ff23663ba47b9b9543f9"

FROZEN_ELO_INITIAL = 1500.0
FROZEN_ELO_K_FACTOR = 45.0
FROZEN_ELO_HOME_ADVANTAGE = 110.0
FROZEN_ELO_DRAW_PARAMETER = 0.50
FROZEN_ELO_WARMUP_DAYS = 1460
FROZEN_DIXON_COLES_RHO = 0.24
FROZEN_POISSON_HOME_MULTIPLIER = 1.08

CONFIRMATION_HOLDOUT_START_DATE = "2026-08-25"
CONFIRMATION_MIN_ELIGIBLE_TARGETS = 100

router = APIRouter(prefix="/research/enigma-rating-v2", tags=["research"])

_FROZEN_MANIFEST: dict[str, Any] = {
    "status": "FROZEN",
    "version": FROZEN_TUNING_VERSION,
    "selection_sha256": FROZEN_SELECTION_SHA256,
    "research_only": True,
    "selection": {
        "window": {
            "start_date": SELECTION_START_DATE,
            "end_date": SELECTION_END_DATE,
            "partition": SELECTION_PARTITION,
            "leagues": list(SELECTION_LEAGUES),
        },
        "objective": {
            "primary": "validation_brier_multiclass",
            "tie_break": "validation_log_loss",
            "lower_is_better": True,
            "existing_observed_test_used_for_selection": False,
        },
        "baseline": {
            "baseline_id": BASELINE_ID,
            "baseline_sha256": BASELINE_SHA256,
            "split_sha256": SPLIT_SHA256,
            "family": "STANDARD",
            "feature_count": 36,
        },
        "coarse_stage": {
            "elo_winner": {
                "parameters": {
                    "elo_k_factor": 30.0,
                    "elo_home_advantage": 95.0,
                    "elo_draw_parameter": 0.50,
                },
                "validation": {
                    "sample_size": 100,
                    "coverage_pct": 98.039,
                    "brier_multiclass": 0.567956,
                    "log_loss": 0.960842,
                    "accuracy": 0.59,
                    "ece_pp": 5.672,
                },
            },
            "dixon_coles_winner": {
                "parameters": {"dixon_coles_rho": 0.12},
                "validation_goals_only": {
                    "sample_size": 102,
                    "coverage_pct": 100.0,
                    "brier_multiclass": 0.595447,
                    "log_loss": 0.996463,
                    "accuracy": 0.480392,
                    "ece_pp": 7.336,
                },
            },
        },
        "refinement_stage": {
            "version": "enigma_rating_v2_tuning_v1_refinement1",
            "one_refinement_stage_only": True,
            "elo_winner": {
                "parameters": {
                    "elo_k_factor": FROZEN_ELO_K_FACTOR,
                    "elo_home_advantage": FROZEN_ELO_HOME_ADVANTAGE,
                    "elo_draw_parameter": FROZEN_ELO_DRAW_PARAMETER,
                    "elo_initial": FROZEN_ELO_INITIAL,
                    "elo_warmup_days": FROZEN_ELO_WARMUP_DAYS,
                },
                "validation": {
                    "targets": 102,
                    "sample_size": 100,
                    "coverage_pct": 98.039,
                    "brier_multiclass": 0.564545,
                    "log_loss": 0.955463,
                    "accuracy": 0.58,
                    "average_probability_actual": 0.430655,
                    "ece_pp": 4.856,
                },
                "winner_on_search_boundary": {
                    "elo_k_factor": True,
                    "elo_home_advantage": False,
                    "elo_draw_parameter": False,
                },
            },
            "dixon_coles_winner": {
                "parameters": {"dixon_coles_rho": FROZEN_DIXON_COLES_RHO},
                "validation_goals_only": {
                    "targets": 102,
                    "sample_size": 102,
                    "coverage_pct": 100.0,
                    "brier_multiclass": 0.594636,
                    "log_loss": 0.995306,
                    "accuracy": 0.480392,
                    "average_probability_actual": 0.395675,
                    "ece_pp": 10.244,
                },
                "validation_xg_xga_secondary": {
                    "targets": 102,
                    "sample_size": 26,
                    "coverage_pct": 25.49,
                    "brier_multiclass": 0.549512,
                    "log_loss": 0.925965,
                    "accuracy": 0.5,
                    "average_probability_actual": 0.413583,
                    "ece_pp": 17.097,
                    "selection_driver": False,
                },
                "winner_on_search_boundary": {"dixon_coles_rho": True},
            },
        },
    },
    "frozen_parameters": {
        "elo": {
            "initial": FROZEN_ELO_INITIAL,
            "k_factor": FROZEN_ELO_K_FACTOR,
            "home_advantage": FROZEN_ELO_HOME_ADVANTAGE,
            "draw_parameter": FROZEN_ELO_DRAW_PARAMETER,
            "warmup_days": FROZEN_ELO_WARMUP_DAYS,
        },
        "dixon_coles": {"rho": FROZEN_DIXON_COLES_RHO},
        "poisson_home_multiplier": FROZEN_POISSON_HOME_MULTIPLIER,
    },
    "confirmation_holdout": {
        "status": "RESERVED_UNTOUCHED",
        "start_date": CONFIRMATION_HOLDOUT_START_DATE,
        "end_date": None,
        "minimum_eligible_targets": CONFIRMATION_MIN_ELIGIBLE_TARGETS,
        "performance_peeking_before_minimum_targets_allowed": False,
        "retuning_with_holdout_data_allowed": False,
        "confirmation_must_use_selection_sha256": FROZEN_SELECTION_SHA256,
    },
    "policy": {
        "no_further_parameter_search_before_confirmation": True,
        "any_parameter_change_creates_new_research_version": True,
        "any_parameter_change_requires_new_future_holdout": True,
        "production_standard_model_unchanged": True,
        "decision_engine_unchanged": True,
        "prediction_and_ledger_persistence_unchanged": True,
        "frozen_boundary_winners_are_hypotheses_for_confirmation_not_proof_of_optimality": True,
    },
}


def frozen_tuning_manifest() -> dict[str, Any]:
    return deepcopy(_FROZEN_MANIFEST)


@router.get("/frozen-v1")
def enigma_rating_v2_frozen_params_endpoint() -> dict[str, Any]:
    return frozen_tuning_manifest()
