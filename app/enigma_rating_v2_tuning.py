from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from itertools import product
from statistics import mean
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import app.enigma_rating_v2_evaluation as evaluation
from app.baseline_1x2 import BASELINE_1X2_VERSION, build_baseline_1x2_temporal_v1
from app.database import SessionLocal
from app.enigma_rating_v2_evaluation_chronology import (
    FIXTURE_PAGE_SIZE,
    RATE_HISTORY_MAX_MATCHES,
    XG_HISTORY_MAX_OBSERVATIONS,
    _append_observation,
    _compose_model_history,
    _fixture_page,
    _latest_snapshot_payloads,
)
from app.football_probability_models import (
    derive_expected_goals,
    dixon_coles_1x2,
    elo_davidson_1x2,
    elo_update,
)
from app.league_registry import canonical_league

ENIGMA_RATING_V2_TUNING_VERSION = "enigma_rating_v2_tuning_v1"
CONFIRMATION_HOLDOUT_START = date(2026, 8, 25)
SELECTION_DATA_MAX_DATE = date(2026, 8, 24)
CONFIRMATION_MIN_TARGETS = 100

DEFAULT_TUNING_START_DATE = date(2026, 1, 1)
DEFAULT_TUNING_END_DATE = SELECTION_DATA_MAX_DATE
DEFAULT_TUNING_LEAGUES = ("Serie A", "Serie B", "Copa Libertadores", "La Liga")
DEFAULT_MAX_ROWS = 2000
DEFAULT_ELO_WARMUP_DAYS = 1460
DEFAULT_POISSON_HOME_MULTIPLIER = 1.08

ELO_K_GRID = (10.0, 15.0, 20.0, 25.0, 30.0)
ELO_HOME_ADVANTAGE_GRID = (35.0, 50.0, 65.0, 80.0, 95.0)
ELO_DRAW_PARAMETER_GRID = (0.50, 0.60, 0.70, 0.80, 0.90)
DIXON_COLES_RHO_GRID = (-0.20, -0.16, -0.12, -0.08, -0.04, 0.00, 0.04, 0.08, 0.12)

router = APIRouter(prefix="/research/enigma-rating-v2", tags=["research"])


def _validation_targets_from_baseline(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    predictions = (baseline.get("predictions") or {}).get("validation") or []
    for prediction in predictions:
        actual = str(prediction.get("actual") or "")
        if actual not in evaluation.CLASS_ORDER:
            continue
        rows.append(
            {
                "fixture_id": int(prediction["fixture_id"]),
                "sportmonks_fixture_id": int(prediction["sportmonks_fixture_id"]),
                "starts_at": prediction["starts_at"],
                "league": prediction["league"],
                "home_team": prediction["home_team"],
                "away_team": prediction["away_team"],
                "actual": actual,
            }
        )
    rows.sort(key=lambda row: (str(row["starts_at"]), int(row["fixture_id"])))
    return rows


def _score_summary(observations: list[dict[str, Any]], targets: int) -> dict[str, Any]:
    if not observations:
        return {
            "targets": int(targets),
            "sample_size": 0,
            "coverage_pct": 0.0 if targets else None,
            "brier_multiclass": None,
            "log_loss": None,
            "accuracy": None,
            "average_probability_actual": None,
            "predicted_class_calibration": evaluation._predicted_class_calibration([]),
        }
    n = len(observations)
    return {
        "targets": int(targets),
        "sample_size": n,
        "coverage_pct": round((n / targets) * 100.0, 3) if targets else None,
        "brier_multiclass": evaluation._round(mean(float(row["brier"]) for row in observations)),
        "log_loss": evaluation._round(mean(float(row["log_loss"]) for row in observations)),
        "accuracy": evaluation._round(mean(float(row["correct"]) for row in observations)),
        "average_probability_actual": evaluation._round(
            mean(float(row["p_actual"]) for row in observations)
        ),
        "predicted_class_calibration": evaluation._predicted_class_calibration(observations),
    }


def _expected_pair(result: dict[str, Any]) -> tuple[float, float] | None:
    if result.get("status") != "ok":
        return None
    expected = result.get("expected_goals") or {}
    try:
        return float(expected["home"]), float(expected["away"])
    except (KeyError, TypeError, ValueError):
        return None


def _build_validation_material(
    validation_targets: list[dict[str, Any]],
    *,
    elo_warmup_days: int,
    poisson_home_multiplier: float,
) -> dict[str, Any]:
    if not validation_targets:
        return {"groups": [], "dc_contexts": [], "audit": {"targets": 0}}

    target_by_fixture_id = {int(row["fixture_id"]): row for row in validation_targets}
    target_times = [
        evaluation._aware_utc(datetime.fromisoformat(str(row["starts_at"])))
        for row in validation_targets
    ]
    earliest_target = min(target_times)
    latest_target = max(target_times)
    warmup_start = earliest_target - timedelta(days=int(elo_warmup_days))
    requested_league_keys = {
        str(canonical_league(str(row.get("league") or "")).get("key"))
        for row in validation_targets
        if canonical_league(str(row.get("league") or "")).get("key")
    }

    rate_histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=RATE_HISTORY_MAX_MATCHES)
    )
    xg_histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=XG_HISTORY_MAX_OBSERVATIONS)
    )

    compact_groups: list[list[dict[str, Any]]] = []
    dc_contexts: list[dict[str, Any]] = []
    audit: Counter[str] = Counter()
    pending_group: list[dict[str, Any]] = []
    pending_group_starts_at: datetime | None = None

    def process_group(group: list[dict[str, Any]]) -> None:
        if not group:
            return

        compact: list[dict[str, Any]] = []
        for event in group:
            fixture = event["fixture"]
            payload = event["payload"]
            league_key = str(canonical_league(fixture.league_name).get("key") or "")
            home_key = (league_key, str(fixture.home_team))
            away_key = (league_key, str(fixture.away_team))
            target = target_by_fixture_id.get(int(fixture.id))

            if target is not None:
                home_history = _compose_model_history(
                    rate_histories[home_key], xg_histories[home_key]
                )
                away_history = _compose_model_history(
                    rate_histories[away_key], xg_histories[away_key]
                )
                rate_ready = (
                    int(home_history["history_matches"]) >= evaluation.MIN_RATE_HISTORY_MATCHES
                    and int(away_history["history_matches"]) >= evaluation.MIN_RATE_HISTORY_MATCHES
                )
                xg_ready = (
                    int(home_history["xg_for_history_matches"]) >= evaluation.MIN_XG_HISTORY_MATCHES
                    and int(home_history["xg_against_history_matches"]) >= evaluation.MIN_XG_HISTORY_MATCHES
                    and int(away_history["xg_for_history_matches"]) >= evaluation.MIN_XG_HISTORY_MATCHES
                    and int(away_history["xg_against_history_matches"]) >= evaluation.MIN_XG_HISTORY_MATCHES
                )

                goals_expected: tuple[float, float] | None = None
                xg_expected: tuple[float, float] | None = None
                if rate_ready:
                    goals_expected = _expected_pair(
                        derive_expected_goals(
                            home_goals_for_avg=home_history["goals_for_avg"],
                            away_goals_for_avg=away_history["goals_for_avg"],
                            home_goals_against_avg=home_history["goals_against_avg"],
                            away_goals_against_avg=away_history["goals_against_avg"],
                            home_advantage_multiplier=float(poisson_home_multiplier),
                        )
                    )
                if rate_ready and xg_ready:
                    xg_expected = _expected_pair(
                        derive_expected_goals(
                            home_goals_for_avg=home_history["goals_for_avg"],
                            away_goals_for_avg=away_history["goals_for_avg"],
                            home_goals_against_avg=home_history["goals_against_avg"],
                            away_goals_against_avg=away_history["goals_against_avg"],
                            home_xg_for_avg=home_history["xg_for_avg"],
                            away_xg_for_avg=away_history["xg_for_avg"],
                            home_xg_against_avg=home_history["xg_against_avg"],
                            away_xg_against_avg=away_history["xg_against_avg"],
                            home_advantage_multiplier=float(poisson_home_multiplier),
                        )
                    )
                dc_contexts.append(
                    {
                        "fixture_id": int(fixture.id),
                        "actual": target["actual"],
                        "league": target["league"],
                        "goals_expected": goals_expected,
                        "xg_expected": xg_expected,
                    }
                )
                audit["dc_targets"] += 1
                audit["dc_goals_ready"] += int(goals_expected is not None)
                audit["dc_xg_ready"] += int(xg_expected is not None)

            compact.append(
                {
                    "fixture_id": int(fixture.id),
                    "starts_at": evaluation._aware_utc(fixture.starts_at),
                    "league_key": league_key,
                    "home_team": str(fixture.home_team),
                    "away_team": str(fixture.away_team),
                    "home_score": (
                        float(payload["home_score"]) if payload is not None else None
                    ),
                }
            )

        compact_groups.append(compact)

        for event in group:
            fixture = event["fixture"]
            payload = event["payload"]
            if payload is None:
                continue
            league_key = str(canonical_league(fixture.league_name).get("key") or "")
            home_key = (league_key, str(fixture.home_team))
            away_key = (league_key, str(fixture.away_team))
            _append_observation(
                rate_history=rate_histories[home_key],
                xg_history=xg_histories[home_key],
                observation=payload["home_observation"],
            )
            _append_observation(
                rate_history=rate_histories[away_key],
                xg_history=xg_histories[away_key],
                observation=payload["away_observation"],
            )
            audit["usable_updates"] += 1

    cursor_starts_at: datetime | None = None
    cursor_fixture_id: int | None = None
    with SessionLocal() as session:
        while True:
            page = _fixture_page(
                session,
                warmup_start=warmup_start,
                latest_target=latest_target,
                cursor_starts_at=cursor_starts_at,
                cursor_fixture_id=cursor_fixture_id,
                limit=FIXTURE_PAGE_SIZE,
            )
            if not page:
                break
            audit["fixture_pages"] += 1
            audit["fixture_metadata_rows_scanned"] += len(page)
            eligible_page = [
                row
                for row in page
                if canonical_league(row.league_name).get("key") in requested_league_keys
            ]
            audit["eligible_fixtures"] += len(eligible_page)
            snapshot_map = _latest_snapshot_payloads(
                session, [int(row.id) for row in eligible_page]
            )
            audit["snapshots_loaded"] += len(snapshot_map)

            for row in eligible_page:
                fixture = SimpleNamespace(
                    id=int(row.id),
                    league_name=row.league_name,
                    home_team=row.home_team,
                    away_team=row.away_team,
                    starts_at=evaluation._aware_utc(row.starts_at),
                )
                payload = evaluation._fixture_payload(
                    fixture, snapshot_map.get(int(fixture.id))
                )
                starts_at = evaluation._aware_utc(fixture.starts_at)
                if (
                    pending_group
                    and pending_group_starts_at is not None
                    and starts_at != pending_group_starts_at
                ):
                    process_group(pending_group)
                    pending_group.clear()
                if not pending_group:
                    pending_group_starts_at = starts_at
                pending_group.append({"fixture": fixture, "payload": payload})

            last = page[-1]
            cursor_starts_at = evaluation._aware_utc(last.starts_at)
            cursor_fixture_id = int(last.id)
            snapshot_map.clear()
            page.clear()

        process_group(pending_group)
        pending_group.clear()

    return {
        "groups": compact_groups,
        "dc_contexts": dc_contexts,
        "audit": {
            "targets": len(validation_targets),
            "earliest_validation_target": earliest_target.isoformat(),
            "latest_validation_target": latest_target.isoformat(),
            "warmup_start": warmup_start.isoformat(),
            "elo_warmup_days": int(elo_warmup_days),
            "fixture_page_size": FIXTURE_PAGE_SIZE,
            "fixture_pages": int(audit["fixture_pages"]),
            "fixture_metadata_rows_scanned": int(audit["fixture_metadata_rows_scanned"]),
            "eligible_fixtures": int(audit["eligible_fixtures"]),
            "snapshots_loaded": int(audit["snapshots_loaded"]),
            "usable_updates": int(audit["usable_updates"]),
            "compact_timestamp_groups": len(compact_groups),
            "dc_goals_ready": int(audit["dc_goals_ready"]),
            "dc_xg_ready": int(audit["dc_xg_ready"]),
            "selection_partition": "validation",
            "same_timestamp_predictions_before_updates": True,
            "test_partition_used_for_selection": False,
        },
    }


def _elo_grid_search(
    groups: list[list[dict[str, Any]]],
    validation_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_by_fixture_id = {int(row["fixture_id"]): row for row in validation_targets}
    candidates: list[dict[str, Any]] = []

    for k_factor, home_advantage, draw_parameter in product(
        ELO_K_GRID, ELO_HOME_ADVANTAGE_GRID, ELO_DRAW_PARAMETER_GRID
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

        metrics = _score_summary(observations, len(validation_targets))
        candidates.append(
            {
                "parameters": {
                    "elo_k_factor": float(k_factor),
                    "elo_home_advantage": float(home_advantage),
                    "elo_draw_parameter": float(draw_parameter),
                },
                "validation": metrics,
            }
        )
    return candidates


def _dixon_coles_grid_search(
    dc_contexts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    targets = len(dc_contexts)
    for rho in DIXON_COLES_RHO_GRID:
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
                "validation_goals_only": _score_summary(goals_observations, targets),
                "validation_xg_xga_secondary": _score_summary(xg_observations, targets),
            }
        )
    return candidates


def _metric_sort_key(candidate: dict[str, Any], metrics_key: str) -> tuple[float, float, str]:
    metrics = candidate.get(metrics_key) or {}
    brier = metrics.get("brier_multiclass")
    log_loss = metrics.get("log_loss")
    return (
        float(brier) if brier is not None else float("inf"),
        float(log_loss) if log_loss is not None else float("inf"),
        str(candidate.get("parameters") or {}),
    )


def _top_candidates(
    candidates: list[dict[str, Any]], metrics_key: str, limit: int = 10
) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda row: _metric_sort_key(row, metrics_key))[:limit]


def build_enigma_rating_v2_tuning_v1(
    *,
    start_date: date = DEFAULT_TUNING_START_DATE,
    end_date: date = DEFAULT_TUNING_END_DATE,
    leagues: list[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    elo_warmup_days: int = DEFAULT_ELO_WARMUP_DAYS,
    poisson_home_multiplier: float = DEFAULT_POISSON_HOME_MULTIPLIER,
    include_grid: bool = False,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if end_date >= CONFIRMATION_HOLDOUT_START:
        raise ValueError(
            "validation tuning cannot use confirmation holdout dates on or after 2026-08-25"
        )
    if max_rows < 30 or max_rows > evaluation.MAX_EVALUATION_ROWS:
        raise ValueError(
            f"max_rows must be between 30 and {evaluation.MAX_EVALUATION_ROWS}"
        )
    selected_leagues = leagues or list(DEFAULT_TUNING_LEAGUES)

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
    validation_targets = _validation_targets_from_baseline(baseline)
    if not validation_targets:
        raise ValueError("validation partition has no eligible targets")

    material = _build_validation_material(
        validation_targets,
        elo_warmup_days=int(elo_warmup_days),
        poisson_home_multiplier=float(poisson_home_multiplier),
    )
    elo_candidates = _elo_grid_search(material["groups"], validation_targets)
    dc_candidates = _dixon_coles_grid_search(material["dc_contexts"])

    elo_ranked = _top_candidates(elo_candidates, "validation", limit=10)
    dc_ranked = _top_candidates(dc_candidates, "validation_goals_only", limit=10)
    elo_winner = elo_ranked[0]
    dc_winner = dc_ranked[0]

    response: dict[str, Any] = {
        "status": "ok",
        "version": ENIGMA_RATING_V2_TUNING_VERSION,
        "research_only": True,
        "selection_window": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "leagues": selected_leagues,
            "partition": "validation",
        },
        "baseline": {
            "version": BASELINE_1X2_VERSION,
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
            "accuracy_is_diagnostic_not_selection_objective": True,
            "test_partition_used_for_selection": False,
        },
        "elo": {
            "grid": {
                "k_factor": list(ELO_K_GRID),
                "home_advantage": list(ELO_HOME_ADVANTAGE_GRID),
                "draw_parameter": list(ELO_DRAW_PARAMETER_GRID),
                "candidate_count": len(elo_candidates),
            },
            "winner": elo_winner,
            "top_candidates": elo_ranked,
        },
        "dixon_coles": {
            "grid": {
                "rho": list(DIXON_COLES_RHO_GRID),
                "candidate_count": len(dc_candidates),
            },
            "selection_basis": (
                "goals-only validation arm with broad coverage; xG/xGA is secondary diagnostic"
            ),
            "winner": dc_winner,
            "top_candidates": dc_ranked,
        },
        "confirmation_holdout": {
            "status": "RESERVED_UNTOUCHED",
            "start_date": CONFIRMATION_HOLDOUT_START.isoformat(),
            "end_date": None,
            "minimum_eligible_targets_before_confirmation": CONFIRMATION_MIN_TARGETS,
            "parameter_changes_after_holdout_start_allowed": False,
            "selection_data_must_end_on_or_before": SELECTION_DATA_MAX_DATE.isoformat(),
            "confirmation_must_use_frozen_parameters": True,
        },
        "audit": material["audit"],
        "policy": {
            "validation_only_tuning": True,
            "existing_observed_test_is_not_used_for_parameter_selection": True,
            "future_confirmation_holdout_starts_after_all_selection_data": True,
            "same_timestamp_leakage_guard_preserved": True,
            "elo_1460_day_warmup_preserved": int(elo_warmup_days) == 1460,
            "dixon_coles_xg_coverage_cannot_drive_rho_selection": True,
            "production_standard_model_unchanged": True,
            "decision_engine_unchanged": True,
            "prediction_and_ledger_persistence_unchanged": True,
        },
    }
    if include_grid:
        response["elo"]["all_candidates"] = elo_candidates
        response["dixon_coles"]["all_candidates"] = dc_candidates
    return response


@router.get("/tuning-v1")
def enigma_rating_v2_tuning_endpoint(
    start_date: date = Query(default=DEFAULT_TUNING_START_DATE),
    end_date: date = Query(default=DEFAULT_TUNING_END_DATE),
    leagues: list[str] | None = Query(default=None),
    max_rows: int = Query(default=DEFAULT_MAX_ROWS, ge=30, le=evaluation.MAX_EVALUATION_ROWS),
    elo_warmup_days: int = Query(default=DEFAULT_ELO_WARMUP_DAYS, ge=180, le=3650),
    poisson_home_multiplier: float = Query(default=DEFAULT_POISSON_HOME_MULTIPLIER, ge=0.8, le=1.3),
    include_grid: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return build_enigma_rating_v2_tuning_v1(
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
