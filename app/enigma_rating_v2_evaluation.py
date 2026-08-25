from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.baseline_1x2 import BASELINE_1X2_VERSION, build_baseline_1x2_temporal_v1
from app.database import SessionLocal
from app.enigma_rating_v2_context import (
    DEFAULT_ELO_INITIAL,
    DEFAULT_ELO_K_FACTOR,
    MIN_ELO_TEAM_MATCHES,
    MIN_RATE_HISTORY_MATCHES,
    MIN_XG_HISTORY_MATCHES,
)
from app.football_probability_models import (
    DEFAULT_DIXON_COLES_RHO,
    DEFAULT_ELO_DRAW_PARAMETER,
    DEFAULT_ELO_HOME_ADVANTAGE,
    derive_expected_goals,
    dixon_coles_1x2,
    elo_davidson_1x2,
    elo_update,
    poisson_1x2,
)
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot
from app.training_dataset import STAT_NAMES, _as_list, _stat_value, _xg_value

ENIGMA_RATING_V2_EVALUATION_VERSION = "enigma_rating_v2_evaluation_v1"
CLASS_ORDER = ("1", "X", "2")
UNIFORM_BRIER_1X2 = 2.0 / 3.0
UNIFORM_LOG_LOSS_1X2 = math.log(3.0)
DEFAULT_ELO_WARMUP_DAYS = 1460
MAX_EVALUATION_ROWS = 2000

MODEL_STANDARD = "STANDARD"
MODEL_POISSON_GOALS = "POISSON_GOALS_ONLY"
MODEL_POISSON_XG = "POISSON_XG_XGA"
MODEL_DC_GOALS = "DIXON_COLES_GOALS_ONLY"
MODEL_DC_XG = "DIXON_COLES_XG_XGA"
MODEL_ELO = "ELO_DAVIDSON"

MODEL_ORDER = (
    MODEL_STANDARD,
    MODEL_POISSON_GOALS,
    MODEL_POISSON_XG,
    MODEL_DC_GOALS,
    MODEL_DC_XG,
    MODEL_ELO,
)

router = APIRouter(prefix="/research/enigma-rating-v2", tags=["research"])


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _probability_triplet(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        raw = {side: float(value[side]) for side in CLASS_ORDER}
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(p) or p < 0.0 for p in raw.values()):
        return None
    total = sum(raw.values())
    if total <= 0.0:
        return None
    return {side: raw[side] / total for side in CLASS_ORDER}


def _probability_observation(probabilities: dict[str, float], actual: str) -> dict[str, float | str]:
    p = _probability_triplet(probabilities)
    if p is None or actual not in CLASS_ORDER:
        raise ValueError("valid 1X2 probabilities and actual result are required")
    predicted = max(CLASS_ORDER, key=lambda side: p[side])
    p_actual = max(1e-12, min(1.0, p[actual]))
    return {
        "actual": actual,
        "predicted": predicted,
        "predicted_confidence": p[predicted],
        "correct": 1.0 if predicted == actual else 0.0,
        "brier": sum(
            (p[side] - (1.0 if side == actual else 0.0)) ** 2
            for side in CLASS_ORDER
        ),
        "log_loss": -math.log(p_actual),
        "p_actual": p[actual],
    }


def _predicted_class_calibration(observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not observations:
        return {
            "sample_size": 0,
            "ece": None,
            "ece_pp": None,
            "mce": None,
            "mce_pp": None,
            "curve": [],
        }
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        confidence = max(0.0, min(1.0, float(row["predicted_confidence"])))
        bucket = min(9, int(confidence * 10.0))
        grouped[bucket].append(row)

    curve: list[dict[str, Any]] = []
    weighted_error = 0.0
    max_error = 0.0
    total = len(observations)
    for bucket in range(10):
        rows = grouped.get(bucket) or []
        if not rows:
            continue
        avg_confidence = sum(float(row["predicted_confidence"]) for row in rows) / len(rows)
        observed_success = sum(float(row["correct"]) for row in rows) / len(rows)
        gap = observed_success - avg_confidence
        weighted_error += (len(rows) / total) * abs(gap)
        max_error = max(max_error, abs(gap))
        curve.append(
            {
                "bucket": f"{bucket * 10}-<{(bucket + 1) * 10}%" if bucket < 9 else "90-100%",
                "sample_size": len(rows),
                "average_confidence": _round(avg_confidence),
                "observed_success_rate": _round(observed_success),
                "calibration_gap": _round(gap),
                "calibration_gap_pp": _round(gap * 100.0, 3),
            }
        )
    return {
        "sample_size": total,
        "ece": _round(weighted_error),
        "ece_pp": _round(weighted_error * 100.0, 3),
        "mce": _round(max_error),
        "mce_pp": _round(max_error * 100.0, 3),
        "curve": curve,
    }


def _quality(rows: list[dict[str, Any]], model_name: str) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for row in rows:
        probabilities = (row.get("models") or {}).get(model_name)
        if probabilities is None:
            continue
        actual = str(row.get("actual") or "")
        if actual not in CLASS_ORDER:
            continue
        try:
            observations.append(_probability_observation(probabilities, actual))
        except ValueError:
            continue

    denominator = len(rows)
    if not observations:
        return {
            "targets": denominator,
            "sample_size": 0,
            "coverage_pct": 0.0 if denominator else None,
            "brier_multiclass": None,
            "log_loss": None,
            "accuracy": None,
            "average_probability_actual": None,
            "skill_vs_uniform": {"brier_skill": None, "log_loss_skill": None},
            "empirical_climatology": {
                "class_rates": {"1": None, "X": None, "2": None},
                "brier_multiclass": None,
                "log_loss": None,
                "skill": {"brier_skill": None, "log_loss_skill": None},
            },
            "predicted_class_calibration": _predicted_class_calibration([]),
        }

    n = len(observations)
    brier = sum(float(item["brier"]) for item in observations) / n
    log_loss = sum(float(item["log_loss"]) for item in observations) / n
    accuracy = sum(float(item["correct"]) for item in observations) / n
    average_p_actual = sum(float(item["p_actual"]) for item in observations) / n
    class_rates = {
        side: sum(1 for item in observations if item["actual"] == side) / n
        for side in CLASS_ORDER
    }
    climatology_brier = 1.0 - sum(rate * rate for rate in class_rates.values())
    climatology_log_loss = -sum(
        rate * math.log(rate)
        for rate in class_rates.values()
        if rate > 0.0
    )
    return {
        "targets": denominator,
        "sample_size": n,
        "coverage_pct": _round((n / denominator) * 100.0, 3) if denominator else None,
        "brier_multiclass": _round(brier),
        "log_loss": _round(log_loss),
        "accuracy": _round(accuracy),
        "average_probability_actual": _round(average_p_actual),
        "skill_vs_uniform": {
            "brier_skill": _round(1.0 - (brier / UNIFORM_BRIER_1X2)),
            "log_loss_skill": _round(1.0 - (log_loss / UNIFORM_LOG_LOSS_1X2)),
        },
        "empirical_climatology": {
            "class_rates": {side: _round(rate) for side, rate in class_rates.items()},
            "brier_multiclass": _round(climatology_brier),
            "log_loss": _round(climatology_log_loss),
            "skill": {
                "brier_skill": _round(1.0 - (brier / climatology_brier)) if climatology_brier > 0.0 else None,
                "log_loss_skill": _round(1.0 - (log_loss / climatology_log_loss)) if climatology_log_loss > 0.0 else None,
            },
        },
        "predicted_class_calibration": _predicted_class_calibration(observations),
    }


def _paired_delta(
    rows: list[dict[str, Any]],
    challenger: str,
    baseline: str = MODEL_STANDARD,
) -> dict[str, Any]:
    common = [
        row
        for row in rows
        if (row.get("models") or {}).get(challenger) is not None
        and (row.get("models") or {}).get(baseline) is not None
    ]
    challenger_quality = _quality(common, challenger)
    baseline_quality = _quality(common, baseline)
    cb = challenger_quality.get("brier_multiclass")
    bb = baseline_quality.get("brier_multiclass")
    cl = challenger_quality.get("log_loss")
    bl = baseline_quality.get("log_loss")
    return {
        "common_sample_size": len(common),
        "challenger": challenger,
        "baseline": baseline,
        "brier_delta_challenger_minus_baseline": _round(float(cb) - float(bb)) if cb is not None and bb is not None else None,
        "log_loss_delta_challenger_minus_baseline": _round(float(cl) - float(bl)) if cl is not None and bl is not None else None,
        "negative_delta_is_better": True,
    }


def _average(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return mean(values) if values else None


def _history_summary(history: deque[dict[str, Any]]) -> dict[str, Any]:
    rows = list(history)
    return {
        "history_matches": len(rows),
        "points_per_match": _average(rows, "points"),
        "goals_for_avg": _average(rows, "goals_for"),
        "goals_against_avg": _average(rows, "goals_against"),
        "xg_for_avg": _average(rows, "xg_for"),
        "xg_against_avg": _average(rows, "xg_against"),
        "xg_for_history_matches": sum(1 for row in rows if row.get("xg_for") is not None),
        "xg_against_history_matches": sum(1 for row in rows if row.get("xg_against") is not None),
    }


def _fixture_payload(
    fixture: Fixture,
    snapshot: FixtureDataSnapshot | None,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    statistics = _as_list(snapshot.statistics)
    xg_rows = _as_list(snapshot.xg)
    if not statistics:
        return None

    home_goals = _stat_value(statistics, STAT_NAMES["goals"], "home")
    away_goals = _stat_value(statistics, STAT_NAMES["goals"], "away")
    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        actual = "1"
        home_score = 1.0
    elif home_goals < away_goals:
        actual = "2"
        home_score = 0.0
    else:
        actual = "X"
        home_score = 0.5

    def observation(side: str, opponent_side: str) -> dict[str, Any]:
        goals_for = float(_stat_value(statistics, STAT_NAMES["goals"], side))
        goals_against = float(_stat_value(statistics, STAT_NAMES["goals"], opponent_side))
        points = 3.0 if goals_for > goals_against else (1.0 if goals_for == goals_against else 0.0)
        return {
            "starts_at": _aware_utc(fixture.starts_at),
            "points": points,
            "goals_for": goals_for,
            "goals_against": goals_against,
            "xg_for": _xg_value(xg_rows, statistics, side),
            "xg_against": _xg_value(xg_rows, statistics, opponent_side),
        }

    return {
        "actual": actual,
        "home_score": home_score,
        "home_observation": observation("home", "away"),
        "away_observation": observation("away", "home"),
    }


def _latest_snapshot_map(session, fixture_ids: list[int]) -> dict[int, FixtureDataSnapshot]:
    if not fixture_ids:
        return {}
    result: dict[int, FixtureDataSnapshot] = {}
    chunk_size = 1000
    for start in range(0, len(fixture_ids), chunk_size):
        chunk = fixture_ids[start : start + chunk_size]
        rows = session.scalars(
            select(FixtureDataSnapshot)
            .where(FixtureDataSnapshot.fixture_id.in_(chunk))
            .order_by(
                FixtureDataSnapshot.fixture_id.asc(),
                FixtureDataSnapshot.fetched_at.desc(),
                FixtureDataSnapshot.id.desc(),
            )
        ).all()
        for row in rows:
            result.setdefault(int(row.fixture_id), row)
    return result


def _expected_goal_models(
    *,
    home_history: dict[str, Any],
    away_history: dict[str, Any],
    dixon_coles_rho: float,
    home_advantage_multiplier: float,
) -> dict[str, dict[str, float] | None]:
    rate_ready = (
        int(home_history["history_matches"]) >= MIN_RATE_HISTORY_MATCHES
        and int(away_history["history_matches"]) >= MIN_RATE_HISTORY_MATCHES
    )
    if not rate_ready:
        return {
            MODEL_POISSON_GOALS: None,
            MODEL_POISSON_XG: None,
            MODEL_DC_GOALS: None,
            MODEL_DC_XG: None,
        }

    goals_only = derive_expected_goals(
        home_goals_for_avg=home_history["goals_for_avg"],
        away_goals_for_avg=away_history["goals_for_avg"],
        home_goals_against_avg=home_history["goals_against_avg"],
        away_goals_against_avg=away_history["goals_against_avg"],
        home_advantage_multiplier=home_advantage_multiplier,
    )
    result: dict[str, dict[str, float] | None] = {
        MODEL_POISSON_GOALS: None,
        MODEL_POISSON_XG: None,
        MODEL_DC_GOALS: None,
        MODEL_DC_XG: None,
    }
    if goals_only.get("status") == "ok":
        expected = goals_only["expected_goals"]
        result[MODEL_POISSON_GOALS] = poisson_1x2(
            expected["home"], expected["away"]
        )["probabilities"]
        result[MODEL_DC_GOALS] = dixon_coles_1x2(
            expected["home"], expected["away"], rho=dixon_coles_rho
        )["probabilities"]

    full_xg_ready = (
        int(home_history["xg_for_history_matches"]) >= MIN_XG_HISTORY_MATCHES
        and int(home_history["xg_against_history_matches"]) >= MIN_XG_HISTORY_MATCHES
        and int(away_history["xg_for_history_matches"]) >= MIN_XG_HISTORY_MATCHES
        and int(away_history["xg_against_history_matches"]) >= MIN_XG_HISTORY_MATCHES
    )
    if not full_xg_ready:
        return result

    xg_blend = derive_expected_goals(
        home_goals_for_avg=home_history["goals_for_avg"],
        away_goals_for_avg=away_history["goals_for_avg"],
        home_goals_against_avg=home_history["goals_against_avg"],
        away_goals_against_avg=away_history["goals_against_avg"],
        home_xg_for_avg=home_history["xg_for_avg"],
        away_xg_for_avg=away_history["xg_for_avg"],
        home_xg_against_avg=home_history["xg_against_avg"],
        away_xg_against_avg=away_history["xg_against_avg"],
        home_advantage_multiplier=home_advantage_multiplier,
    )
    if xg_blend.get("status") == "ok":
        expected = xg_blend["expected_goals"]
        result[MODEL_POISSON_XG] = poisson_1x2(
            expected["home"], expected["away"]
        )["probabilities"]
        result[MODEL_DC_XG] = dixon_coles_1x2(
            expected["home"], expected["away"], rho=dixon_coles_rho
        )["probabilities"]
    return result


def _evaluate_challengers_chronologically(
    targets: list[dict[str, Any]],
    *,
    dixon_coles_rho: float,
    elo_initial: float,
    elo_k_factor: float,
    elo_home_advantage: float,
    elo_draw_parameter: float,
    poisson_home_multiplier: float,
    elo_warmup_days: int,
) -> dict[str, Any]:
    if not targets:
        return {"rows": [], "audit": {"fixtures_scanned": 0, "snapshots_loaded": 0}}

    target_by_fixture_id = {int(row["fixture_id"]): row for row in targets}
    target_times = [_aware_utc(datetime.fromisoformat(str(row["starts_at"]))) for row in targets]
    earliest_target = min(target_times)
    latest_target = max(target_times)
    warmup_start = earliest_target - timedelta(days=int(elo_warmup_days))

    requested_league_keys = {
        str(canonical_league(str(row.get("league") or "")).get("key"))
        for row in targets
        if canonical_league(str(row.get("league") or "")).get("key")
    }

    with SessionLocal() as session:
        fixtures = session.scalars(
            select(Fixture)
            .where(
                Fixture.starts_at >= warmup_start,
                Fixture.starts_at <= latest_target,
            )
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()
        fixtures = [
            fixture
            for fixture in fixtures
            if canonical_league(fixture.league_name).get("key") in requested_league_keys
        ]
        snapshot_map = _latest_snapshot_map(session, [int(fixture.id) for fixture in fixtures])

    histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=10))
    ratings: dict[tuple[str, str], float] = {}
    elo_matches: dict[tuple[str, str], int] = defaultdict(int)
    output: dict[int, dict[str, Any]] = {}
    payload_cache = {
        int(fixture.id): _fixture_payload(fixture, snapshot_map.get(int(fixture.id)))
        for fixture in fixtures
    }

    index = 0
    while index < len(fixtures):
        starts_at = _aware_utc(fixtures[index].starts_at)
        group: list[Fixture] = []
        while index < len(fixtures) and _aware_utc(fixtures[index].starts_at) == starts_at:
            group.append(fixtures[index])
            index += 1

        for fixture in group:
            target = target_by_fixture_id.get(int(fixture.id))
            if target is None:
                continue
            league = canonical_league(fixture.league_name)
            league_key = str(league.get("key") or "")
            home_key = (league_key, str(fixture.home_team))
            away_key = (league_key, str(fixture.away_team))
            home_history = _history_summary(histories[home_key])
            away_history = _history_summary(histories[away_key])

            models = dict(target.get("models") or {})
            models.update(
                _expected_goal_models(
                    home_history=home_history,
                    away_history=away_history,
                    dixon_coles_rho=float(dixon_coles_rho),
                    home_advantage_multiplier=float(poisson_home_multiplier),
                )
            )

            home_elo_ready = elo_matches[home_key] >= MIN_ELO_TEAM_MATCHES
            away_elo_ready = elo_matches[away_key] >= MIN_ELO_TEAM_MATCHES
            if home_elo_ready and away_elo_ready:
                models[MODEL_ELO] = elo_davidson_1x2(
                    ratings.get(home_key, float(elo_initial)),
                    ratings.get(away_key, float(elo_initial)),
                    home_advantage_elo=float(elo_home_advantage),
                    draw_parameter=float(elo_draw_parameter),
                )["probabilities"]
            else:
                models[MODEL_ELO] = None

            form10_ready = len(histories[home_key]) == 10 and len(histories[away_key]) == 10
            form10_delta = None
            if form10_ready:
                home_ppm = home_history["points_per_match"]
                away_ppm = away_history["points_per_match"]
                if home_ppm is not None and away_ppm is not None:
                    form10_delta = float(home_ppm) - float(away_ppm)

            output[int(fixture.id)] = {
                **target,
                "models": models,
                "context_audit": {
                    "history_strictly_before_target": True,
                    "same_timestamp_group_updated_after_all_predictions": True,
                    "home_history_matches": home_history["history_matches"],
                    "away_history_matches": away_history["history_matches"],
                    "home_xg_for_history_matches": home_history["xg_for_history_matches"],
                    "home_xg_against_history_matches": home_history["xg_against_history_matches"],
                    "away_xg_for_history_matches": away_history["xg_for_history_matches"],
                    "away_xg_against_history_matches": away_history["xg_against_history_matches"],
                    "home_elo_matches": elo_matches[home_key],
                    "away_elo_matches": elo_matches[away_key],
                    "form10_ready": form10_ready,
                    "form10_points_per_match_delta": _round(form10_delta),
                },
            }

        for fixture in group:
            payload = payload_cache.get(int(fixture.id))
            if payload is None:
                continue
            league = canonical_league(fixture.league_name)
            league_key = str(league.get("key") or "")
            home_key = (league_key, str(fixture.home_team))
            away_key = (league_key, str(fixture.away_team))
            histories[home_key].append(payload["home_observation"])
            histories[away_key].append(payload["away_observation"])

            home_rating = ratings.get(home_key, float(elo_initial))
            away_rating = ratings.get(away_key, float(elo_initial))
            new_home, new_away = elo_update(
                home_rating,
                away_rating,
                home_score=float(payload["home_score"]),
                k_factor=float(elo_k_factor),
                home_advantage_elo=float(elo_home_advantage),
            )
            ratings[home_key] = new_home
            ratings[away_key] = new_away
            elo_matches[home_key] += 1
            elo_matches[away_key] += 1

    rows = [output[int(row["fixture_id"])] for row in targets if int(row["fixture_id"]) in output]
    return {
        "rows": rows,
        "audit": {
            "fixtures_scanned": len(fixtures),
            "snapshots_loaded": len(snapshot_map),
            "targets_requested": len(targets),
            "targets_evaluated": len(rows),
            "warmup_start": warmup_start.isoformat(),
            "earliest_target": earliest_target.isoformat(),
            "latest_target": latest_target.isoformat(),
            "elo_history_policy": "expanding pre-target Elo initialized at evaluation warmup start",
            "elo_warmup_days": int(elo_warmup_days),
            "same_timestamp_targets_are_scored_before_any_same_timestamp_result_update": True,
        },
    }


def _targets_from_baseline(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for partition in ("validation", "test"):
        for prediction in (baseline.get("predictions") or {}).get(partition) or []:
            probabilities = _probability_triplet(prediction.get("probabilities"))
            if probabilities is None:
                continue
            rows.append(
                {
                    "fixture_id": int(prediction["fixture_id"]),
                    "sportmonks_fixture_id": int(prediction["sportmonks_fixture_id"]),
                    "starts_at": prediction["starts_at"],
                    "league": prediction["league"],
                    "home_team": prediction["home_team"],
                    "away_team": prediction["away_team"],
                    "actual": str(prediction["actual"]),
                    "partition": partition,
                    "models": {MODEL_STANDARD: probabilities},
                }
            )
    rows.sort(key=lambda row: (str(row["starts_at"]), int(row["fixture_id"])))
    return rows


def _partition_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    model_metrics = {model_name: _quality(rows, model_name) for model_name in MODEL_ORDER}
    pairwise = {
        model_name: _paired_delta(rows, model_name)
        for model_name in MODEL_ORDER
        if model_name != MODEL_STANDARD
    }
    return {
        "targets": len(rows),
        "models": model_metrics,
        "paired_vs_standard": pairwise,
    }


def _league_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("league") or "UNKNOWN")].append(row)
    return {
        league: {
            "targets": len(league_rows),
            "models": {model_name: _quality(league_rows, model_name) for model_name in MODEL_ORDER},
        }
        for league, league_rows in sorted(grouped.items())
    }


def _month_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        starts_at = str(row.get("starts_at") or "")
        month = starts_at[:7] if len(starts_at) >= 7 else "UNKNOWN"
        grouped[month].append(row)
    return {
        month: {
            "targets": len(month_rows),
            "models": {model_name: _quality(month_rows, model_name) for model_name in MODEL_ORDER},
        }
        for month, month_rows in sorted(grouped.items())
    }


def _xg_ablation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "poisson": _paired_delta(rows, MODEL_POISSON_XG, baseline=MODEL_POISSON_GOALS),
        "dixon_coles": _paired_delta(rows, MODEL_DC_XG, baseline=MODEL_DC_GOALS),
        "interpretation": "negative delta means the full xG/xGA blend improved the metric versus goals-only on the same fixtures",
    }


def _form10_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if bool((row.get("context_audit") or {}).get("form10_ready"))
    ]
    return {
        "eligible_targets": len(eligible),
        "coverage_pct": _round((len(eligible) / len(rows)) * 100.0, 3) if rows else None,
        "models_on_form10_ready_targets": {
            model_name: _quality(eligible, model_name) for model_name in MODEL_ORDER
        },
        "policy": {
            "form10_is_not_converted_into_an_arbitrary_probability_model": True,
            "this_is_a_readiness_and_performance_slice_not_a_promoted_ablation_model": True,
        },
    }


def build_enigma_rating_v2_evaluation_v1(
    *,
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    lookback_matches: int = 5,
    min_history_matches: int = 3,
    train_ratio: float = 0.70,
    validation_ratio: float = 0.15,
    max_rows: int = 1000,
    dixon_coles_rho: float = DEFAULT_DIXON_COLES_RHO,
    elo_initial: float = DEFAULT_ELO_INITIAL,
    elo_k_factor: float = DEFAULT_ELO_K_FACTOR,
    elo_home_advantage: float = DEFAULT_ELO_HOME_ADVANTAGE,
    elo_draw_parameter: float = DEFAULT_ELO_DRAW_PARAMETER,
    poisson_home_multiplier: float = 1.08,
    elo_warmup_days: int = DEFAULT_ELO_WARMUP_DAYS,
    include_rows: bool = False,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if max_rows < 30 or max_rows > MAX_EVALUATION_ROWS:
        raise ValueError(f"max_rows must be between 30 and {MAX_EVALUATION_ROWS}")
    if elo_warmup_days < 180 or elo_warmup_days > 3650:
        raise ValueError("elo_warmup_days must be between 180 and 3650")
    if elo_k_factor <= 0.0 or elo_k_factor > 80.0:
        raise ValueError("elo_k_factor must be > 0 and <= 80")

    baseline = build_baseline_1x2_temporal_v1(
        start_date=start_date,
        end_date=end_date,
        leagues=leagues,
        family="STANDARD",
        lookback_matches=lookback_matches,
        min_history_matches=min_history_matches,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        max_rows=max_rows,
        class_weight_balanced=False,
        include_predictions=True,
    )
    targets = _targets_from_baseline(baseline)
    challenger_result = _evaluate_challengers_chronologically(
        targets,
        dixon_coles_rho=float(dixon_coles_rho),
        elo_initial=float(elo_initial),
        elo_k_factor=float(elo_k_factor),
        elo_home_advantage=float(elo_home_advantage),
        elo_draw_parameter=float(elo_draw_parameter),
        poisson_home_multiplier=float(poisson_home_multiplier),
        elo_warmup_days=int(elo_warmup_days),
    )
    rows = challenger_result["rows"]
    validation_rows = [row for row in rows if row.get("partition") == "validation"]
    test_rows = [row for row in rows if row.get("partition") == "test"]

    response: dict[str, Any] = {
        "status": "ok",
        "version": ENIGMA_RATING_V2_EVALUATION_VERSION,
        "research_only": True,
        "date_range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "leagues": leagues or [],
        "baseline": {
            "version": BASELINE_1X2_VERSION,
            "baseline_id": baseline.get("baseline_id"),
            "baseline_sha256": baseline.get("baseline_sha256"),
            "family": baseline.get("family"),
            "feature_count": (baseline.get("training") or {}).get("feature_count"),
            "split_sha256": (baseline.get("parent") or {}).get("split_sha256"),
        },
        "parameters": {
            "standard_lookback_matches": int(lookback_matches),
            "standard_min_history_matches": int(min_history_matches),
            "train_ratio": float(train_ratio),
            "validation_ratio": float(validation_ratio),
            "dixon_coles_rho": float(dixon_coles_rho),
            "elo_initial": float(elo_initial),
            "elo_k_factor": float(elo_k_factor),
            "elo_home_advantage": float(elo_home_advantage),
            "elo_draw_parameter": float(elo_draw_parameter),
            "poisson_home_multiplier": float(poisson_home_multiplier),
            "elo_warmup_days": int(elo_warmup_days),
        },
        "evaluation": {
            "primary_partition": "test",
            "validation": _partition_report(validation_rows),
            "test": _partition_report(test_rows),
            "test_by_league": _league_breakdown(test_rows),
            "test_by_month": _month_breakdown(test_rows),
            "test_xg_xga_ablation": _xg_ablation(test_rows),
            "test_form10_diagnostic": _form10_diagnostic(test_rows),
        },
        "audit": challenger_result["audit"],
        "policy": {
            "research_only": True,
            "production_standard_model_unchanged": True,
            "decision_engine_unchanged": True,
            "prediction_and_ledger_persistence_unchanged": True,
            "standard_family_remains_36_features": True,
            "standard_is_fit_on_train_only": True,
            "validation_and_test_are_strictly_after_train": True,
            "test_is_primary_final_holdout": True,
            "challenger_context_uses_only_strictly_pre_target_results": True,
            "same_timestamp_results_never_feed_other_same_timestamp_targets": True,
            "target_postgame_data_is_used_only_after_scoring_to_update_future_history": True,
            "xg_missing_is_not_zero": True,
            "xg_xga_ablation_requires_full_xg_and_xga_evidence_for_both_teams": True,
            "dixon_coles_is_low_score_adjustment_not_full_fitted_attack_defence_model": True,
            "no_ensemble_weights_are_tuned_here": True,
            "no_hyperparameter_promotion_is_performed_here": True,
            "form10_is_diagnostic_only_until_a_learned_probability_ablation_is_defined": True,
        },
    }
    if include_rows:
        response["rows"] = rows
    return response


@router.get("/evaluation-v1")
def enigma_rating_v2_evaluation_endpoint(
    start_date: date = Query(...),
    end_date: date = Query(...),
    leagues: list[str] | None = Query(default=None),
    lookback_matches: int = Query(default=5, ge=1, le=10),
    min_history_matches: int = Query(default=3, ge=1, le=10),
    train_ratio: float = Query(default=0.70, gt=0.0, lt=1.0),
    validation_ratio: float = Query(default=0.15, gt=0.0, lt=1.0),
    max_rows: int = Query(default=1000, ge=30, le=MAX_EVALUATION_ROWS),
    dixon_coles_rho: float = Query(default=DEFAULT_DIXON_COLES_RHO, ge=-0.30, le=0.30),
    elo_k_factor: float = Query(default=DEFAULT_ELO_K_FACTOR, gt=0.0, le=80.0),
    elo_home_advantage: float = Query(default=DEFAULT_ELO_HOME_ADVANTAGE, ge=-200.0, le=300.0),
    elo_draw_parameter: float = Query(default=DEFAULT_ELO_DRAW_PARAMETER, gt=0.0, le=3.0),
    poisson_home_multiplier: float = Query(default=1.08, ge=0.8, le=1.3),
    elo_warmup_days: int = Query(default=DEFAULT_ELO_WARMUP_DAYS, ge=180, le=3650),
    include_rows: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return build_enigma_rating_v2_evaluation_v1(
            start_date=start_date,
            end_date=end_date,
            leagues=leagues,
            lookback_matches=lookback_matches,
            min_history_matches=min_history_matches,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
            max_rows=max_rows,
            dixon_coles_rho=dixon_coles_rho,
            elo_k_factor=elo_k_factor,
            elo_home_advantage=elo_home_advantage,
            elo_draw_parameter=elo_draw_parameter,
            poisson_home_multiplier=poisson_home_multiplier,
            elo_warmup_days=elo_warmup_days,
            include_rows=include_rows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "failed", "error": exc.__class__.__name__},
        ) from exc
