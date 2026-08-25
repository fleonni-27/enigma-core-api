from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, or_, select

import app.enigma_rating_v2_evaluation as evaluation
from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture, FixtureDataSnapshot

EVALUATION_XG_CHRONOLOGY_VERSION = "evaluation_v1_xg_chronology_v1"
EVALUATION_LOADER_VERSION = "evaluation_v1_streaming_loader_v1"
RATE_HISTORY_MAX_MATCHES = 10
XG_HISTORY_MAX_OBSERVATIONS = 10
FIXTURE_PAGE_SIZE = 96


def _has_complete_xg(observation: dict[str, Any]) -> bool:
    return (
        observation.get("xg_for") is not None
        and observation.get("xg_against") is not None
    )


def _compose_model_history(
    rate_history: deque[dict[str, Any]],
    xg_history: deque[dict[str, Any]],
) -> dict[str, Any]:
    """Combine recent-result rates with independently accumulated xG evidence."""

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


def _fixture_page(
    session,
    *,
    warmup_start: datetime,
    latest_target: datetime,
    cursor_starts_at: datetime | None,
    cursor_fixture_id: int | None,
    limit: int = FIXTURE_PAGE_SIZE,
) -> list[Any]:
    stmt = select(
        Fixture.id,
        Fixture.league_name,
        Fixture.home_team,
        Fixture.away_team,
        Fixture.starts_at,
    ).where(
        Fixture.starts_at >= warmup_start,
        Fixture.starts_at <= latest_target,
    )
    if cursor_starts_at is not None and cursor_fixture_id is not None:
        stmt = stmt.where(
            or_(
                Fixture.starts_at > cursor_starts_at,
                and_(
                    Fixture.starts_at == cursor_starts_at,
                    Fixture.id > cursor_fixture_id,
                ),
            )
        )
    return list(
        session.execute(
            stmt.order_by(Fixture.starts_at.asc(), Fixture.id.asc()).limit(limit)
        ).all()
    )


def _latest_snapshot_payloads(
    session,
    fixture_ids: list[int],
) -> dict[int, Any]:
    """Load only latest statistics/xG JSON for one bounded fixture page."""

    if not fixture_ids:
        return {}

    ranked = (
        select(
            FixtureDataSnapshot.fixture_id.label("fixture_id"),
            FixtureDataSnapshot.id.label("snapshot_id"),
            FixtureDataSnapshot.statistics.label("statistics"),
            FixtureDataSnapshot.xg.label("xg"),
            func.row_number()
            .over(
                partition_by=FixtureDataSnapshot.fixture_id,
                order_by=[
                    FixtureDataSnapshot.fetched_at.desc(),
                    FixtureDataSnapshot.id.desc(),
                ],
            )
            .label("snapshot_rank"),
        )
        .where(FixtureDataSnapshot.fixture_id.in_(fixture_ids))
        .subquery()
    )
    rows = session.execute(
        select(
            ranked.c.fixture_id,
            ranked.c.snapshot_id,
            ranked.c.statistics,
            ranked.c.xg,
        ).where(ranked.c.snapshot_rank == 1)
    ).all()

    return {
        int(row.fixture_id): SimpleNamespace(
            id=int(row.snapshot_id),
            statistics=row.statistics,
            xg=row.xg,
        )
        for row in rows
    }


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
                "loader_version": EVALUATION_LOADER_VERSION,
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

    rate_histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=RATE_HISTORY_MAX_MATCHES)
    )
    xg_histories: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
        lambda: deque(maxlen=XG_HISTORY_MAX_OBSERVATIONS)
    )
    ratings: dict[tuple[str, str], float] = {}
    elo_matches: dict[tuple[str, str], int] = defaultdict(int)
    output: dict[int, dict[str, Any]] = {}

    audit_counts: Counter[str] = Counter()
    readiness_by_partition: dict[str, Counter[str]] = defaultdict(Counter)
    pending_group: list[dict[str, Any]] = []
    pending_group_starts_at: datetime | None = None

    def process_group(group: list[dict[str, Any]]) -> None:
        if not group:
            return

        for event in group:
            fixture = event["fixture"]
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

        for event in group:
            fixture = event["fixture"]
            payload = event["payload"]
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

            audit_counts["fixture_pages"] += 1
            audit_counts["fixture_metadata_rows_scanned"] += len(page)
            audit_counts["max_fixture_page_rows"] = max(
                audit_counts["max_fixture_page_rows"],
                len(page),
            )

            eligible_page = [
                row
                for row in page
                if canonical_league(row.league_name).get("key")
                in requested_league_keys
            ]
            audit_counts["eligible_fixtures_scanned"] += len(eligible_page)

            fixture_ids = [int(row.id) for row in eligible_page]
            snapshot_map = _latest_snapshot_payloads(session, fixture_ids)
            audit_counts["snapshots_loaded"] += len(snapshot_map)
            audit_counts["max_latest_snapshots_in_page"] = max(
                audit_counts["max_latest_snapshots_in_page"],
                len(snapshot_map),
            )

            for row in eligible_page:
                fixture = SimpleNamespace(
                    id=int(row.id),
                    league_name=row.league_name,
                    home_team=row.home_team,
                    away_team=row.away_team,
                    starts_at=evaluation._aware_utc(row.starts_at),
                )
                snapshot = snapshot_map.get(int(fixture.id))
                if snapshot is not None and evaluation._as_list(snapshot.xg):
                    audit_counts["snapshots_with_nonempty_xg"] += 1

                payload = evaluation._fixture_payload(fixture, snapshot)
                if payload is not None:
                    audit_counts["usable_historical_payloads"] += 1
                    if _has_complete_xg(
                        payload["home_observation"]
                    ) and _has_complete_xg(payload["away_observation"]):
                        audit_counts["payloads_with_full_parsed_xg"] += 1

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
                pending_group.append(
                    {
                        "fixture": fixture,
                        "payload": payload,
                    }
                )

            last = page[-1]
            cursor_starts_at = evaluation._aware_utc(last.starts_at)
            cursor_fixture_id = int(last.id)

            snapshot_map.clear()
            page.clear()

        process_group(pending_group)
        pending_group.clear()

    rows = [
        output[int(row["fixture_id"])]
        for row in targets
        if int(row["fixture_id"]) in output
    ]

    readiness: dict[str, Any] = {}
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
            "fixtures_scanned": int(audit_counts["eligible_fixtures_scanned"]),
            "fixture_metadata_rows_scanned": int(
                audit_counts["fixture_metadata_rows_scanned"]
            ),
            "fixture_pages": int(audit_counts["fixture_pages"]),
            "fixture_page_size": FIXTURE_PAGE_SIZE,
            "max_fixture_page_rows": int(audit_counts["max_fixture_page_rows"]),
            "snapshots_loaded": int(audit_counts["snapshots_loaded"]),
            "max_latest_snapshots_in_page": int(
                audit_counts["max_latest_snapshots_in_page"]
            ),
            "snapshots_with_nonempty_xg": int(
                audit_counts["snapshots_with_nonempty_xg"]
            ),
            "usable_historical_payloads": int(
                audit_counts["usable_historical_payloads"]
            ),
            "payloads_with_full_parsed_xg": int(
                audit_counts["payloads_with_full_parsed_xg"]
            ),
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
            "rate_history_policy": (
                "last 10 usable same-league completed results strictly before target"
            ),
            "xg_history_policy": (
                "last 10 complete same-league xG/xGA observations strictly before target"
            ),
            "xg_missing_matches_do_not_evict_valid_xg_evidence": True,
            "xg_chronology_version": EVALUATION_XG_CHRONOLOGY_VERSION,
            "loader_version": EVALUATION_LOADER_VERSION,
            "loader_policy": {
                "fixture_scan": "keyset paginated chronological SQL",
                "league_canonicalization_before_snapshot_json_load": True,
                "latest_snapshot_only_per_fixture_page": True,
                "lineups_not_loaded": True,
                "older_snapshots_not_materialized": True,
                "raw_snapshot_objects_retained_across_pages": False,
                "same_timestamp_group_may_span_page_boundary": True,
            },
        },
    }


def install_evaluation_v1_xg_chronology_fix() -> None:
    """Install corrected xG chronology and bounded 1460-day loader."""

    evaluation._evaluate_challengers_chronologically = (
        _evaluate_challengers_chronologically
    )
