from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

import app.enigma_rating_v2_evaluation as evaluation
from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture

EVALUATION_XG_CHRONOLOGY_VERSION = "evaluation_v1_xg_chronology_v1"
RATE_HISTORY_MAX_MATCHES = 10
XG_HISTORY_MAX_OBSERVATIONS = 10


def _has_complete_xg(observation: dict[str, Any]) -> bool:
    return (
        observation.get("xg_for") is not None
        and observation.get("xg_against") is not None
    )


def _compose_model_history(
    rate_history: deque[dict[str, Any]],
    xg_history: deque[dict[str, Any]],
) -> dict[str, Any]:
    """Combine recent-result rates with independently accumulated xG evidence.

    Form/goals use the last 10 completed results. xG/xGA uses the last 10
    *complete xG observations*. Missing-xG matches therefore do not evict valid
    xG evidence. Both streams remain chronological and strictly pre-target.
    """

    rate_summary = evaluation._history_summary(rate_history)
    xg_summary = evaluation._history_summary(xg_history)
    return {
        **rate_summary,
        "xg_for_avg": xg_summary["xg_for_avg"],
        "xg_against_avg": xg_summary["xg_against_avg"],
        "xg_for_history_matches": xg_summary["xg_for_history_matches"],
        "xg_against_history_matches": xg_summary["xg_against_history_matches"],
    }


def _append_observation(
    *,
    rate_history: deque[dict[str, Any]],
    xg_history: deque[dict[str, Any]],
    observation: dict[str, Any],
) -> bool:
    rate_history.append(observation)
    if not _has_complete_xg(observation):
        return False
    xg_history.append(observation)
    return True


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
        return {
            "rows": [],
            "audit": {
                "fixtures_scanned": 0,
                "snapshots_loaded": 0,
                "xg_chronology_version": EVALUATION_XG_CHRONOLOGY_VERSION,
            },
        }

    target_by_fixture_id = {int(row["fixture_id"]): row for row in targets}
    target_times = [
        evaluation._aware_utc(datetime.fromisoformat(str(row["starts_at"])))
        for row in targets
    ]
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
            if canonical_league(fixture.league_name).get("key")
            in requested_league_keys
        ]
        snapshot_map = evaluation._latest_snapshot_map(
            session,
            [int(fixture.id) for fixture in fixtures],
        )

    rate_histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=RATE_HISTORY_MAX_MATCHES)
    )
    xg_histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=XG_HISTORY_MAX_OBSERVATIONS)
    )
    ratings: dict[tuple[str, str], float] = {}
    elo_matches: dict[tuple[str, str], int] = defaultdict(int)
    output: dict[int, dict[str, Any]] = {}

    payload_cache = {
        int(fixture.id): evaluation._fixture_payload(
            fixture,
            snapshot_map.get(int(fixture.id)),
        )
        for fixture in fixtures
    }

    snapshot_xg_rows = 0
    payloads_ready = 0
    payloads_with_full_parsed_xg = 0
    for fixture in fixtures:
        snapshot = snapshot_map.get(int(fixture.id))
        if snapshot is not None and evaluation._as_list(snapshot.xg):
            snapshot_xg_rows += 1
        payload = payload_cache.get(int(fixture.id))
        if payload is None:
            continue
        payloads_ready += 1
        home_observation = payload["home_observation"]
        away_observation = payload["away_observation"]
        if _has_complete_xg(home_observation) and _has_complete_xg(away_observation):
            payloads_with_full_parsed_xg += 1

    audit_counts: Counter[str] = Counter()
    readiness_by_partition: dict[str, Counter[str]] = defaultdict(Counter)

    index = 0
    while index < len(fixtures):
        starts_at = evaluation._aware_utc(fixtures[index].starts_at)
        group: list[Fixture] = []
        while (
            index < len(fixtures)
            and evaluation._aware_utc(fixtures[index].starts_at) == starts_at
        ):
            group.append(fixtures[index])
            index += 1

        # Score every target at this timestamp before appending any result from
        # the same timestamp. This preserves the existing leakage guard.
        for fixture in group:
            target = target_by_fixture_id.get(int(fixture.id))
            if target is None:
                continue

            league = canonical_league(fixture.league_name)
            league_key = str(league.get("key") or "")
            home_key = (league_key, str(fixture.home_team))
            away_key = (league_key, str(fixture.away_team))

            home_history = _compose_model_history(
                rate_histories[home_key],
                xg_histories[home_key],
            )
            away_history = _compose_model_history(
                rate_histories[away_key],
                xg_histories[away_key],
            )

            models = dict(target.get("models") or {})
            models.update(
                evaluation._expected_goal_models(
                    home_history=home_history,
                    away_history=away_history,
                    dixon_coles_rho=float(dixon_coles_rho),
                    home_advantage_multiplier=float(poisson_home_multiplier),
                )
            )

            home_elo_ready = (
                elo_matches[home_key] >= evaluation.MIN_ELO_TEAM_MATCHES
            )
            away_elo_ready = (
                elo_matches[away_key] >= evaluation.MIN_ELO_TEAM_MATCHES
            )
            if home_elo_ready and away_elo_ready:
                models[evaluation.MODEL_ELO] = evaluation.elo_davidson_1x2(
                    ratings.get(home_key, float(elo_initial)),
                    ratings.get(away_key, float(elo_initial)),
                    home_advantage_elo=float(elo_home_advantage),
                    draw_parameter=float(elo_draw_parameter),
                )["probabilities"]
            else:
                models[evaluation.MODEL_ELO] = None

            form10_ready = (
                len(rate_histories[home_key]) == RATE_HISTORY_MAX_MATCHES
                and len(rate_histories[away_key]) == RATE_HISTORY_MAX_MATCHES
            )
            form10_delta = None
            if form10_ready:
                home_ppm = home_history["points_per_match"]
                away_ppm = away_history["points_per_match"]
                if home_ppm is not None and away_ppm is not None:
                    form10_delta = float(home_ppm) - float(away_ppm)

            full_xg_ready = (
                int(home_history["xg_for_history_matches"])
                >= evaluation.MIN_XG_HISTORY_MATCHES
                and int(home_history["xg_against_history_matches"])
                >= evaluation.MIN_XG_HISTORY_MATCHES
                and int(away_history["xg_for_history_matches"])
                >= evaluation.MIN_XG_HISTORY_MATCHES
                and int(away_history["xg_against_history_matches"])
                >= evaluation.MIN_XG_HISTORY_MATCHES
            )
            any_xg_history = (
                int(home_history["xg_for_history_matches"]) > 0
                or int(away_history["xg_for_history_matches"]) > 0
            )

            partition = str(target.get("partition") or "unknown")
            audit_counts["targets_scored"] += 1
            readiness_by_partition[partition]["targets"] += 1
            if any_xg_history:
                audit_counts["targets_with_any_xg_history"] += 1
                readiness_by_partition[partition]["with_any_xg_history"] += 1
            if full_xg_ready:
                audit_counts["targets_with_full_xg_history_ready"] += 1
                readiness_by_partition[partition]["full_xg_history_ready"] += 1

            output[int(fixture.id)] = {
                **target,
                "models": models,
                "context_audit": {
                    "history_strictly_before_target": True,
                    "same_timestamp_group_updated_after_all_predictions": True,
                    "home_history_matches": home_history["history_matches"],
                    "away_history_matches": away_history["history_matches"],
                    "home_xg_for_history_matches": home_history[
                        "xg_for_history_matches"
                    ],
                    "home_xg_against_history_matches": home_history[
                        "xg_against_history_matches"
                    ],
                    "away_xg_for_history_matches": away_history[
                        "xg_for_history_matches"
                    ],
                    "away_xg_against_history_matches": away_history[
                        "xg_against_history_matches"
                    ],
                    "home_elo_matches": elo_matches[home_key],
                    "away_elo_matches": elo_matches[away_key],
                    "form10_ready": form10_ready,
                    "form10_points_per_match_delta": evaluation._round(
                        form10_delta
                    ),
                    "xg_full_history_ready": full_xg_ready,
                    "rate_history_window_matches": RATE_HISTORY_MAX_MATCHES,
                    "xg_history_window_observations": XG_HISTORY_MAX_OBSERVATIONS,
                    "xg_missing_matches_do_not_evict_valid_xg_evidence": True,
                },
            }

        # Only after all predictions at this timestamp have been scored do we
        # append the observed results to the histories used by future targets.
        for fixture in group:
            payload = payload_cache.get(int(fixture.id))
            if payload is None:
                audit_counts["fixtures_without_usable_payload"] += 1
                continue

            league = canonical_league(fixture.league_name)
            league_key = str(league.get("key") or "")
            home_key = (league_key, str(fixture.home_team))
            away_key = (league_key, str(fixture.away_team))

            home_xg_appended = _append_observation(
                rate_history=rate_histories[home_key],
                xg_history=xg_histories[home_key],
                observation=payload["home_observation"],
            )
            away_xg_appended = _append_observation(
                rate_history=rate_histories[away_key],
                xg_history=xg_histories[away_key],
                observation=payload["away_observation"],
            )
            audit_counts["team_rate_observations_appended"] += 2
            audit_counts["team_complete_xg_observations_appended"] += int(
                home_xg_appended
            ) + int(away_xg_appended)

            home_rating = ratings.get(home_key, float(elo_initial))
            away_rating = ratings.get(away_key, float(elo_initial))
            new_home, new_away = evaluation.elo_update(
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

    rows = [
        output[int(row["fixture_id"])]
        for row in targets
        if int(row["fixture_id"]) in output
    ]

    readiness = {}
    for partition, counts in sorted(readiness_by_partition.items()):
        total = int(counts["targets"])
        full_ready = int(counts["full_xg_history_ready"])
        any_ready = int(counts["with_any_xg_history"])
        readiness[partition] = {
            "targets": total,
            "with_any_xg_history": any_ready,
            "with_any_xg_history_pct": (
                round(any_ready / total * 100.0, 3) if total else None
            ),
            "full_xg_history_ready": full_ready,
            "full_xg_history_ready_pct": (
                round(full_ready / total * 100.0, 3) if total else None
            ),
        }

    return {
        "rows": rows,
        "audit": {
            "fixtures_scanned": len(fixtures),
            "snapshots_loaded": len(snapshot_map),
            "snapshots_with_nonempty_xg": snapshot_xg_rows,
            "usable_historical_payloads": payloads_ready,
            "payloads_with_full_parsed_xg": payloads_with_full_parsed_xg,
            "team_rate_observations_appended": int(
                audit_counts["team_rate_observations_appended"]
            ),
            "team_complete_xg_observations_appended": int(
                audit_counts["team_complete_xg_observations_appended"]
            ),
            "targets_requested": len(targets),
            "targets_evaluated": len(rows),
            "targets_with_any_xg_history": int(
                audit_counts["targets_with_any_xg_history"]
            ),
            "targets_with_full_xg_history_ready": int(
                audit_counts["targets_with_full_xg_history_ready"]
            ),
            "xg_readiness_by_partition": readiness,
            "warmup_start": warmup_start.isoformat(),
            "earliest_target": earliest_target.isoformat(),
            "latest_target": latest_target.isoformat(),
            "elo_history_policy": (
                "expanding pre-target Elo initialized at evaluation warmup start"
            ),
            "elo_warmup_days": int(elo_warmup_days),
            "same_timestamp_targets_are_scored_before_any_same_timestamp_result_update": True,
            "snapshot_selection_policy": "latest fetched_at DESC, id DESC per fixture",
            "rate_history_policy": "last 10 usable same-league completed results strictly before target",
            "xg_history_policy": "last 10 complete same-league xG/xGA observations strictly before target",
            "xg_missing_matches_do_not_evict_valid_xg_evidence": True,
            "xg_chronology_version": EVALUATION_XG_CHRONOLOGY_VERSION,
        },
    }


def install_evaluation_v1_xg_chronology_fix() -> None:
    """Install the corrected chronology without changing the public V1 route."""

    evaluation._evaluate_challengers_chronologically = (
        _evaluate_challengers_chronologically
    )
