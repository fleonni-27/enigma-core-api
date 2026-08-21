from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.data_quality import assess_fixture_quality
from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture

MAX_FEATURE_FIXTURES_PER_RUN = 200

PROFILE_FULL_XG = "FULL_XG"
PROFILE_STANDARD_NO_XG = "STANDARD_NO_XG"
PROFILE_INCOMPLETE = "INCOMPLETE"
PROFILE_NO_SNAPSHOT = "NO_SNAPSHOT"


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    return keys


def classify_feature_profile_from_assessment(assessment: dict) -> dict:
    if assessment.get("status") == "fixture_not_found":
        return {
            "profile": PROFILE_NO_SNAPSHOT,
            "snapshot_available": False,
            "training_eligible": False,
            "features": {"lineups": False, "statistics": False, "xg": False},
            "reason": "fixture_not_found",
        }

    blockers = list(assessment.get("blockers") or [])
    snapshot = assessment.get("snapshot")
    coverage = assessment.get("coverage") or {}

    snapshot_available = bool(snapshot) and "missing_snapshot" not in blockers
    lineups = int(((coverage.get("lineups") or {}).get("records", 0)) or 0) > 0
    statistics = int(((coverage.get("statistics") or {}).get("records", 0)) or 0) > 0
    xg = int(((coverage.get("xg") or {}).get("records", 0)) or 0) > 0
    training_eligible = bool(assessment.get("approved_for_training"))

    if not snapshot_available:
        profile = PROFILE_NO_SNAPSHOT
        reason = "missing_snapshot"
    elif lineups and statistics and xg:
        profile = PROFILE_FULL_XG
        reason = "lineups_statistics_xg_available"
    elif lineups and statistics and not xg:
        profile = PROFILE_STANDARD_NO_XG
        reason = "lineups_statistics_available_xg_unavailable"
    else:
        profile = PROFILE_INCOMPLETE
        reason = "essential_feature_layer_missing"

    return {
        "profile": profile,
        "snapshot_available": snapshot_available,
        "training_eligible": training_eligible,
        "features": {
            "lineups": lineups,
            "statistics": statistics,
            "xg": xg,
        },
        "reason": reason,
    }


def classify_fixture_feature_profile(sportmonks_fixture_id: int) -> dict:
    assessment = assess_fixture_quality(sportmonks_fixture_id)
    classification = classify_feature_profile_from_assessment(assessment)
    return {
        "status": assessment.get("status", "ok"),
        "sportmonks_fixture_id": sportmonks_fixture_id,
        "fixture": assessment.get("fixture"),
        "snapshot": assessment.get("snapshot"),
        "quality_score": assessment.get("quality_score", 0.0),
        "decision": assessment.get("decision"),
        "warnings": assessment.get("warnings") or [],
        "blockers": assessment.get("blockers") or [],
        **classification,
        "policy": {
            "profile_is_fixture_based": True,
            "xg_absence_is_zero": False,
            "full_xg_requires": ["lineups", "statistics", "xg"],
            "standard_no_xg_requires": ["lineups", "statistics"],
        },
    }


def build_feature_profile_report(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = 100,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_FEATURE_FIXTURES_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_FEATURE_FIXTURES_PER_RUN}")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys = _requested_league_keys(leagues)

    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    fixtures: list[Fixture] = []
    for fixture in candidates:
        canonical = canonical_league(fixture.league_name)
        if requested_keys and canonical.get("key") not in requested_keys:
            continue
        fixtures.append(fixture)
        if len(fixtures) >= limit:
            break

    profile_counts: Counter[str] = Counter()
    enriched_profiles: Counter[str] = Counter()
    training_eligible = 0
    results: list[dict] = []
    league_acc: dict[str, Counter[str]] = defaultdict(Counter)

    for fixture in fixtures:
        assessment = assess_fixture_quality(int(fixture.sportmonks_id))
        classification = classify_feature_profile_from_assessment(assessment)
        canonical = canonical_league(fixture.league_name)
        league_name = str(canonical.get("canonical_name") or fixture.league_name or "Unknown")
        profile = str(classification["profile"])

        profile_counts[profile] += 1
        league_acc[league_name][profile] += 1
        league_acc[league_name]["fixtures"] += 1

        if classification["snapshot_available"]:
            enriched_profiles[profile] += 1
            league_acc[league_name]["enriched"] += 1
        if classification["training_eligible"]:
            training_eligible += 1
            league_acc[league_name]["training_eligible"] += 1

        results.append(
            {
                "sportmonks_fixture_id": fixture.sportmonks_id,
                "fixture_id": fixture.id,
                "league": league_name,
                "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "profile": profile,
                "snapshot_available": bool(classification["snapshot_available"]),
                "training_eligible": bool(classification["training_eligible"]),
                "features": classification["features"],
                "quality_score": float(assessment.get("quality_score", 0.0) or 0.0),
                "decision": assessment.get("decision"),
                "warnings": assessment.get("warnings") or [],
                "blockers": assessment.get("blockers") or [],
            }
        )

    total = len(fixtures)
    enriched = total - int(profile_counts.get(PROFILE_NO_SNAPSHOT, 0))

    def pct(value: int, denominator: int) -> float:
        return round(value / denominator * 100.0, 1) if denominator else 0.0

    by_league: list[dict] = []
    for league_name, bucket in league_acc.items():
        fixtures_count = int(bucket["fixtures"])
        enriched_count = int(bucket["enriched"])
        by_league.append(
            {
                "league": league_name,
                "fixtures": fixtures_count,
                "enriched": enriched_count,
                "coverage_rate_pct": pct(enriched_count, fixtures_count),
                "FULL_XG": int(bucket[PROFILE_FULL_XG]),
                "STANDARD_NO_XG": int(bucket[PROFILE_STANDARD_NO_XG]),
                "INCOMPLETE": int(bucket[PROFILE_INCOMPLETE]),
                "NO_SNAPSHOT": int(bucket[PROFILE_NO_SNAPSHOT]),
                "full_xg_pct_among_snapshots": pct(int(bucket[PROFILE_FULL_XG]), enriched_count),
                "standard_no_xg_pct_among_snapshots": pct(int(bucket[PROFILE_STANDARD_NO_XG]), enriched_count),
                "incomplete_pct_among_snapshots": pct(int(bucket[PROFILE_INCOMPLETE]), enriched_count),
                "training_eligible": int(bucket["training_eligible"]),
                "training_eligibility_pct_among_snapshots": pct(int(bucket["training_eligible"]), enriched_count),
            }
        )
    by_league.sort(key=lambda row: (-row["fixtures"], row["league"]))

    return {
        "status": "ok",
        "version": "feature_profile_v1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "limit": limit,
        "selected_fixtures": total,
        "summary": {
            "fixtures_in_scope": total,
            "enriched_fixtures": enriched,
            "missing_snapshots": int(profile_counts.get(PROFILE_NO_SNAPSHOT, 0)),
            "coverage_rate_pct": pct(enriched, total),
            "profiles": {
                PROFILE_FULL_XG: int(profile_counts.get(PROFILE_FULL_XG, 0)),
                PROFILE_STANDARD_NO_XG: int(profile_counts.get(PROFILE_STANDARD_NO_XG, 0)),
                PROFILE_INCOMPLETE: int(profile_counts.get(PROFILE_INCOMPLETE, 0)),
                PROFILE_NO_SNAPSHOT: int(profile_counts.get(PROFILE_NO_SNAPSHOT, 0)),
            },
            "profile_pct_among_snapshots": {
                PROFILE_FULL_XG: pct(int(enriched_profiles.get(PROFILE_FULL_XG, 0)), enriched),
                PROFILE_STANDARD_NO_XG: pct(int(enriched_profiles.get(PROFILE_STANDARD_NO_XG, 0)), enriched),
                PROFILE_INCOMPLETE: pct(int(enriched_profiles.get(PROFILE_INCOMPLETE, 0)), enriched),
            },
            "training_eligible": training_eligible,
            "training_eligibility_pct_among_snapshots": pct(training_eligible, enriched),
        },
        "by_league": by_league,
        "results": results,
        "policy": {
            "profile_is_fixture_based": True,
            "xg_absence_is_zero": False,
            "FULL_XG": "Snapshot has lineups, statistics and xG.",
            "STANDARD_NO_XG": "Snapshot has lineups and statistics; xG is unavailable and must remain missing, never zero-imputed.",
            "INCOMPLETE": "Snapshot exists but an essential structural layer is missing.",
            "NO_SNAPSHOT": "Fixture exists but has not been enriched yet.",
            "batch_limit_max": MAX_FEATURE_FIXTURES_PER_RUN,
        },
    }
