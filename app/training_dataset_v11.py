from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.feature_profiles import classify_fixture_feature_profile
from app.league_registry import TARGET_LEAGUES, canonical_league
from app.models import Fixture
from app.training_dataset import (
    MAX_LOOKBACK_MATCHES,
    _aggregate_history,
    _delta,
    _target_label,
    _team_history,
)

TRAINING_DATASET_VERSION = "training_dataset_v1.1"
MAX_PAGE_SIZE = 200
MAX_SKIP_DETAILS = 200
VALID_PROFILES = {"FULL_XG", "STANDARD_NO_XG"}


def _requested_league_context(leagues: list[str] | None) -> tuple[set[str], set[str]]:
    requested_keys: set[str] = set()
    observed_names: set[str] = set()
    for name in leagues or []:
        item = canonical_league(name)
        key = item.get("key")
        if not item.get("target") or not key:
            continue
        requested_keys.add(str(key))
        definition = TARGET_LEAGUES.get(str(key)) or {}
        observed_names.update(str(alias) for alias in definition.get("aliases", []))
        canonical_name = definition.get("canonical_name")
        if canonical_name:
            observed_names.add(str(canonical_name))
    return requested_keys, observed_names


def _skip_detail(fixture: Fixture, reason: str, **extra) -> dict:
    canonical = canonical_league(fixture.league_name)
    result = {
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "fixture_id": int(fixture.id),
        "league": canonical.get("canonical_name") or fixture.league_name,
        "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "reason": reason,
    }
    result.update(extra)
    return result


def _build_row(session, fixture: Fixture, lookback_matches: int, min_history_matches: int) -> tuple[dict | None, str | None, dict | None]:
    canonical = canonical_league(fixture.league_name)
    league_key = canonical.get("key")

    classification = classify_fixture_feature_profile(int(fixture.sportmonks_id))
    profile = str(classification.get("profile") or "")
    if not classification.get("training_eligible") or profile not in VALID_PROFILES:
        return None, "not_training_eligible", _skip_detail(
            fixture,
            "not_training_eligible",
            profile=profile or None,
            training_eligible=bool(classification.get("training_eligible")),
        )

    label = _target_label(session, fixture)
    if label is None:
        return None, "missing_label", _skip_detail(fixture, "missing_label")

    home_history = _team_history(session, fixture, fixture.home_team, league_key, lookback_matches)
    away_history = _team_history(session, fixture, fixture.away_team, league_key, lookback_matches)
    if len(home_history) < min_history_matches or len(away_history) < min_history_matches:
        return None, "insufficient_history", _skip_detail(
            fixture,
            "insufficient_history",
            home_history_matches=len(home_history),
            away_history_matches=len(away_history),
            minimum_required=min_history_matches,
            requested_lookback=lookback_matches,
        )

    historical_times = [row.get("starts_at") for row in home_history + away_history if row.get("starts_at") is not None]
    if any(value >= fixture.starts_at for value in historical_times):
        return None, "leakage_violation", _skip_detail(fixture, "leakage_violation")

    home = _aggregate_history(home_history, fixture.starts_at)
    away = _aggregate_history(away_history, fixture.starts_at)
    home["history_completeness_ratio"] = round(len(home_history) / lookback_matches, 4)
    away["history_completeness_ratio"] = round(len(away_history) / lookback_matches, 4)

    features = {
        "home": home,
        "away": away,
        "delta": {
            "points_per_match": _delta(home, away, "points_per_match"),
            "goals_for_avg": _delta(home, away, "goals_for_avg"),
            "goals_against_avg": _delta(home, away, "goals_against_avg"),
            "shots_total_for_avg": _delta(home, away, "shots_total_for_avg"),
            "shots_on_target_for_avg": _delta(home, away, "shots_on_target_for_avg"),
            "possession_avg": _delta(home, away, "possession_avg"),
            "corners_for_avg": _delta(home, away, "corners_for_avg"),
            "successful_passes_for_avg": _delta(home, away, "successful_passes_for_avg"),
            "xg_for_avg": _delta(home, away, "xg_for_avg"),
            "rest_days": _delta(home, away, "rest_days"),
            "history_completeness_ratio": _delta(home, away, "history_completeness_ratio"),
        },
    }

    row = {
        "sportmonks_fixture_id": int(fixture.sportmonks_id),
        "fixture_id": int(fixture.id),
        "league": canonical.get("canonical_name") or fixture.league_name,
        "starts_at": fixture.starts_at.isoformat(),
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "source_profile": profile,
        "features": features,
        "label": label,
        "leakage_audit": {
            "target_statistics_used_as_features": False,
            "history_strictly_before_target": True,
        },
    }
    return row, None, None


def build_training_dataset_v11(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    page: int = 1,
    page_size: int = 100,
    lookback_matches: int = 5,
    min_history_matches: int = 3,
    include_skipped_details: bool = False,
    skipped_detail_limit: int = 50,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if lookback_matches < 1 or lookback_matches > MAX_LOOKBACK_MATCHES:
        raise ValueError(f"lookback_matches must be between 1 and {MAX_LOOKBACK_MATCHES}")
    if min_history_matches < 1 or min_history_matches > lookback_matches:
        raise ValueError("min_history_matches must be between 1 and lookback_matches")
    if skipped_detail_limit < 0 or skipped_detail_limit > MAX_SKIP_DETAILS:
        raise ValueError(f"skipped_detail_limit must be between 0 and {MAX_SKIP_DETAILS}")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys, requested_names = _requested_league_context(leagues)

    with SessionLocal() as session:
        stmt = select(Fixture).where(Fixture.starts_at.between(start_dt, end_dt))
        # Push known league aliases into SQL so isolated-league requests do not scan unrelated fixtures.
        if requested_names:
            stmt = stmt.where(Fixture.league_name.in_(sorted(requested_names)))
        candidates = session.scalars(stmt.order_by(Fixture.starts_at.asc(), Fixture.id.asc())).all()

        all_rows: list[dict] = []
        skipped = {
            "not_target_league": 0,
            "not_training_eligible": 0,
            "missing_label": 0,
            "insufficient_history": 0,
            "leakage_violation": 0,
        }
        skipped_details: list[dict] = []
        profile_counts = {"FULL_XG": 0, "STANDARD_NO_XG": 0}

        for fixture in candidates:
            canonical = canonical_league(fixture.league_name)
            league_key = canonical.get("key")
            if requested_keys and league_key not in requested_keys:
                skipped["not_target_league"] += 1
                if include_skipped_details and len(skipped_details) < skipped_detail_limit:
                    skipped_details.append(_skip_detail(fixture, "not_target_league"))
                continue

            row, reason, detail = _build_row(session, fixture, lookback_matches, min_history_matches)
            if row is None:
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                if include_skipped_details and detail and len(skipped_details) < skipped_detail_limit:
                    skipped_details.append(detail)
                continue

            all_rows.append(row)
            profile = str(row["source_profile"])
            profile_counts[profile] = profile_counts.get(profile, 0) + 1

    total_rows = len(all_rows)
    total_pages = (total_rows + page_size - 1) // page_size if total_rows else 0
    offset = (page - 1) * page_size
    page_rows = all_rows[offset : offset + page_size]

    xg_ready_total = sum(
        1 for row in all_rows
        if row["features"]["home"].get("xg_for_avg") is not None
        and row["features"]["away"].get("xg_for_avg") is not None
    )
    xg_ready_page = sum(
        1 for row in page_rows
        if row["features"]["home"].get("xg_for_avg") is not None
        and row["features"]["away"].get("xg_for_avg") is not None
    )

    response = {
        "status": "ok",
        "version": TRAINING_DATASET_VERSION,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_training_rows": total_rows,
            "total_pages": total_pages,
            "returned_rows": len(page_rows),
            "has_previous": page > 1 and total_rows > 0,
            "has_next": page < total_pages,
            "deterministic_order": "starts_at ASC, fixture_id ASC",
        },
        "summary": {
            "candidate_fixtures_after_sql_filter": len(candidates),
            "training_rows_total": total_rows,
            "source_profiles_total": profile_counts,
            "xg_feature_ready_rows_total": xg_ready_total,
            "xg_feature_ready_pct_total": round((xg_ready_total / total_rows) * 100, 1) if total_rows else 0,
            "xg_feature_ready_rows_page": xg_ready_page,
            "leakage_violations": skipped.get("leakage_violation", 0),
            "skipped": skipped,
        },
        "feature_schema": {
            "history_scope": "same canonical league and strictly earlier fixtures only",
            "rolling_window_matches": lookback_matches,
            "minimum_history_matches": min_history_matches,
            "team_features": [
                "history_matches",
                "history_completeness_ratio",
                "points_per_match",
                "win_rate",
                "draw_rate",
                "goals_for_avg",
                "goals_against_avg",
                "shots_total_for_avg",
                "shots_on_target_for_avg",
                "possession_avg",
                "corners_for_avg",
                "successful_passes_for_avg",
                "xg_for_avg",
                "xg_history_matches",
                "rest_days",
            ],
            "labels": ["outcome_1x2", "home_goals", "away_goals", "total_goals", "btts", "over_2_5"],
        },
        "rows": page_rows,
        "policy": {
            "read_only": True,
            "training_eligible_only": True,
            "excluded_profiles": ["INCOMPLETE", "NO_SNAPSHOT"],
            "upstream_unavailable_excluded": True,
            "target_match_postgame_data_as_features": False,
            "target_match_postgame_data_allowed_for_labels_only": True,
            "history_cutoff_rule": "historical fixture starts_at must be strictly less than target fixture starts_at",
            "xg_absence_is_zero": False,
            "xg_missing_value": None,
            "source_profile_is_metadata_not_model_feature": True,
            "pagination_prevents_first_n_sampling_bias": True,
            "league_filter_applied_at_sql_when_aliases_are_known": True,
        },
    }
    if include_skipped_details:
        response["skipped_details"] = {
            "returned": len(skipped_details),
            "limit": skipped_detail_limit,
            "items": skipped_details,
        }
    return response
