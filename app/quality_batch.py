from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from statistics import mean

from sqlalchemy import select

from app.data_quality import assess_fixture_quality
from app.database import SessionLocal
from app.league_registry import canonical_league
from app.models import Fixture

MAX_QUALITY_FIXTURES_PER_RUN = 200


def _requested_league_keys(leagues: list[str] | None) -> set[str]:
    keys: set[str] = set()
    for league in leagues or []:
        canonical = canonical_league(league)
        if canonical.get("target") and canonical.get("key"):
            keys.add(str(canonical["key"]))
    return keys


def _pct(value: int, denominator: int) -> float:
    return round(value / denominator * 100.0, 1) if denominator else 0.0


def _average(values: list[float]) -> float:
    return round(mean(values), 1) if values else 0.0


def build_quality_batch_report(
    start_date: date,
    end_date: date,
    leagues: list[str] | None = None,
    limit: int = 100,
) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if limit < 1 or limit > MAX_QUALITY_FIXTURES_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_QUALITY_FIXTURES_PER_RUN}")

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

    decision_counts: Counter[str] = Counter()
    blocker_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    snapshot_scores: list[float] = []
    enriched = 0
    eligible = 0
    clean_approved = 0
    warning_snapshots = 0
    rejected_snapshots = 0
    missing_snapshots = 0
    xg_available = 0
    lineups_available = 0
    statistics_available = 0
    results: list[dict] = []

    league_acc: dict[str, dict] = defaultdict(
        lambda: {
            "fixtures": 0,
            "enriched": 0,
            "missing_snapshots": 0,
            "eligible": 0,
            "clean_approved": 0,
            "warning": 0,
            "rejected": 0,
            "scores": [],
            "lineups_available": 0,
            "statistics_available": 0,
            "xg_available": 0,
        }
    )

    for fixture in fixtures:
        assessment = assess_fixture_quality(int(fixture.sportmonks_id))
        score = float(assessment.get("quality_score", 0.0) or 0.0)
        decision = str(assessment.get("decision") or "rejected")
        blockers = list(assessment.get("blockers") or [])
        warnings = list(assessment.get("warnings") or [])
        coverage = assessment.get("coverage") or {}
        snapshot_available = "missing_snapshot" not in blockers

        decision_counts[decision] += 1
        blocker_counts.update(blockers)
        warning_counts.update(warnings)

        xg_records = int(((coverage.get("xg") or {}).get("records", 0)) or 0)
        lineup_records = int(((coverage.get("lineups") or {}).get("records", 0)) or 0)
        statistic_records = int(((coverage.get("statistics") or {}).get("records", 0)) or 0)

        if snapshot_available:
            enriched += 1
            snapshot_scores.append(score)
            if assessment.get("approved_for_training"):
                eligible += 1
            if decision == "approved_for_training":
                clean_approved += 1
            elif decision == "warning":
                warning_snapshots += 1
            else:
                rejected_snapshots += 1
            if xg_records > 0:
                xg_available += 1
            if lineup_records > 0:
                lineups_available += 1
            if statistic_records > 0:
                statistics_available += 1
        else:
            missing_snapshots += 1

        canonical = canonical_league(fixture.league_name)
        league_name = str(canonical.get("canonical_name") or fixture.league_name or "Unknown")
        bucket = league_acc[league_name]
        bucket["fixtures"] += 1
        if snapshot_available:
            bucket["enriched"] += 1
            bucket["scores"].append(score)
            if assessment.get("approved_for_training"):
                bucket["eligible"] += 1
            if decision == "approved_for_training":
                bucket["clean_approved"] += 1
            elif decision == "warning":
                bucket["warning"] += 1
            else:
                bucket["rejected"] += 1
            bucket["lineups_available"] += 1 if lineup_records > 0 else 0
            bucket["statistics_available"] += 1 if statistic_records > 0 else 0
            bucket["xg_available"] += 1 if xg_records > 0 else 0
        else:
            bucket["missing_snapshots"] += 1

        results.append(
            {
                "sportmonks_fixture_id": fixture.sportmonks_id,
                "fixture_id": fixture.id,
                "league": league_name,
                "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "snapshot_available": snapshot_available,
                "quality_score": score,
                "decision": decision,
                "approved_for_training": bool(assessment.get("approved_for_training")),
                "xg_available": xg_records > 0,
                "lineups_available": lineup_records > 0,
                "statistics_available": statistic_records > 0,
                "blockers": blockers,
                "warnings": warnings,
            }
        )

    evaluated = len(results)

    by_league = []
    for league_name, bucket in league_acc.items():
        fixtures_count = int(bucket["fixtures"])
        enriched_count = int(bucket["enriched"])
        league_scores = list(bucket["scores"])
        by_league.append(
            {
                "league": league_name,
                "fixtures": fixtures_count,
                "enriched": enriched_count,
                "missing_snapshots": int(bucket["missing_snapshots"]),
                "coverage_rate_pct": _pct(enriched_count, fixtures_count),
                "training_eligible": int(bucket["eligible"]),
                "training_eligibility_rate_pct": _pct(int(bucket["eligible"]), enriched_count),
                "clean_approved": int(bucket["clean_approved"]),
                "clean_approval_rate_pct": _pct(int(bucket["clean_approved"]), enriched_count),
                "warning": int(bucket["warning"]),
                "warning_rate_pct": _pct(int(bucket["warning"]), enriched_count),
                "rejected_after_snapshot": int(bucket["rejected"]),
                "snapshot_rejection_rate_pct": _pct(int(bucket["rejected"]), enriched_count),
                "snapshot_quality_score_avg": _average(league_scores),
                "snapshot_quality_score_min": round(min(league_scores), 1) if league_scores else 0.0,
                "snapshot_quality_score_max": round(max(league_scores), 1) if league_scores else 0.0,
                "lineups_coverage_among_snapshots_pct": _pct(int(bucket["lineups_available"]), enriched_count),
                "statistics_coverage_among_snapshots_pct": _pct(int(bucket["statistics_available"]), enriched_count),
                "xg_coverage_among_snapshots_pct": _pct(int(bucket["xg_available"]), enriched_count),
            }
        )
    by_league.sort(key=lambda row: (-row["fixtures"], row["league"]))

    return {
        "status": "ok",
        "version": "quality_batch_v2",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "limit": limit,
        "selected_fixtures": len(fixtures),
        "evaluated": evaluated,
        "summary": {
            "fixtures_in_scope": evaluated,
            "enriched_fixtures": enriched,
            "missing_snapshots": missing_snapshots,
            "coverage_rate_pct": _pct(enriched, evaluated),
            "training_eligible": eligible,
            "training_eligibility_rate_pct": _pct(eligible, enriched),
            "clean_approved": clean_approved,
            "clean_approval_rate_pct": _pct(clean_approved, enriched),
            "warning_snapshots": warning_snapshots,
            "warning_rate_pct": _pct(warning_snapshots, enriched),
            "rejected_after_snapshot": rejected_snapshots,
            "snapshot_rejection_rate_pct": _pct(rejected_snapshots, enriched),
            "snapshot_quality_score_avg": _average(snapshot_scores),
            "snapshot_quality_score_min": round(min(snapshot_scores), 1) if snapshot_scores else 0.0,
            "snapshot_quality_score_max": round(max(snapshot_scores), 1) if snapshot_scores else 0.0,
            "lineups_coverage_among_snapshots_pct": _pct(lineups_available, enriched),
            "statistics_coverage_among_snapshots_pct": _pct(statistics_available, enriched),
            "xg_coverage_among_snapshots_pct": _pct(xg_available, enriched),
        },
        "legacy_decisions": {
            "approved_for_training_decision": int(decision_counts.get("approved_for_training", 0)),
            "warning_decision": int(decision_counts.get("warning", 0)),
            "rejected_decision_including_missing_snapshot": int(decision_counts.get("rejected", 0)),
        },
        "top_blockers": [
            {"name": name, "count": count}
            for name, count in blocker_counts.most_common(10)
        ],
        "top_warnings": [
            {"name": name, "count": count}
            for name, count in warning_counts.most_common(10)
        ],
        "by_league": by_league,
        "results": results,
        "policy": {
            "batch_limit_max": MAX_QUALITY_FIXTURES_PER_RUN,
            "training_scale_target_eligibility_pct": 90.0,
            "metric_definition": "Coverage measures ingestion completeness. Eligibility, approval, rejection and quality scores are calculated only among fixtures with stored snapshots.",
            "xg_absence_is_zero": False,
        },
    }
