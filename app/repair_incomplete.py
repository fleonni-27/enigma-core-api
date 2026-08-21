from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import select

from app.data_quality import assess_fixture_quality
from app.database import SessionLocal
from app.feature_profiles import classify_feature_profile_from_assessment
from app.fixture_data_ingestion import ingest_fixture_data_payload
from app.league_registry import canonical_league
from app.models import Fixture
from app.sportmonks import SportmonksClient

MAX_REPAIRS_PER_RUN = 25


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    return keys


def _is_incomplete_assessment(assessment: dict) -> bool:
    classification = classify_feature_profile_from_assessment(assessment)
    return classification.get("profile") == "INCOMPLETE"


async def repair_incomplete_fixtures(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = 10,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_REPAIRS_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_REPAIRS_PER_RUN}")

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    requested_keys = _requested_league_keys(leagues)

    with SessionLocal() as session:
        candidates = session.scalars(
            select(Fixture)
            .where(Fixture.starts_at.between(start_dt, end_dt))
            .order_by(Fixture.starts_at.asc(), Fixture.id.asc())
        ).all()

    targets: list[Fixture] = []
    for fixture in candidates:
        canonical = canonical_league(fixture.league_name)
        if requested_keys and canonical.get("key") not in requested_keys:
            continue
        assessment = assess_fixture_quality(int(fixture.sportmonks_id))
        if _is_incomplete_assessment(assessment):
            targets.append(fixture)
        if len(targets) >= limit:
            break

    client = SportmonksClient()
    repaired = 0
    still_incomplete = 0
    failed = 0
    results: list[dict] = []

    for fixture in targets:
        before = assess_fixture_quality(int(fixture.sportmonks_id))
        before_profile = classify_feature_profile_from_assessment(before)
        try:
            payload = await client.enriched_fixture(int(fixture.sportmonks_id))
            ingest_result = ingest_fixture_data_payload(int(fixture.sportmonks_id), payload)
            after = assess_fixture_quality(int(fixture.sportmonks_id))
            after_profile = classify_feature_profile_from_assessment(after)

            if after_profile.get("profile") == "INCOMPLETE":
                still_incomplete += 1
                repair_status = "upstream_data_unavailable_or_still_incomplete"
            else:
                repaired += 1
                repair_status = "repaired"

            results.append(
                {
                    "sportmonks_fixture_id": fixture.sportmonks_id,
                    "fixture_id": fixture.id,
                    "league": canonical_league(fixture.league_name).get("canonical_name") or fixture.league_name,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "status": repair_status,
                    "snapshot_id": ingest_result.get("snapshot_id"),
                    "before": {
                        "quality_score": before.get("quality_score", 0),
                        "decision": before.get("decision"),
                        "blockers": before.get("blockers") or [],
                        "warnings": before.get("warnings") or [],
                        "profile": before_profile.get("profile"),
                    },
                    "after": {
                        "quality_score": after.get("quality_score", 0),
                        "decision": after.get("decision"),
                        "blockers": after.get("blockers") or [],
                        "warnings": after.get("warnings") or [],
                        "profile": after_profile.get("profile"),
                        "training_eligible": bool(after.get("approved_for_training")),
                    },
                }
            )
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "sportmonks_fixture_id": fixture.sportmonks_id,
                    "fixture_id": fixture.id,
                    "league": canonical_league(fixture.league_name).get("canonical_name") or fixture.league_name,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "status": "failed",
                    "error": exc.__class__.__name__,
                }
            )

    status = "ok"
    if failed > 0 or still_incomplete > 0:
        status = "partial"

    return {
        "status": status,
        "version": "repair_incomplete_v1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "limit": limit,
        "selected_incomplete": len(targets),
        "repaired": repaired,
        "still_incomplete": still_incomplete,
        "failed": failed,
        "results": results,
        "policy": {
            "healthy_snapshots_are_never_touched": True,
            "repair_only_profile": "INCOMPLETE",
            "repair_strategy": "fetch_fresh_sportmonks_payload_and_append_new_snapshot",
            "latest_snapshot_wins_quality_assessment": True,
            "xg_absence_is_zero": False,
        },
    }
