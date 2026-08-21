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
    scores: list[float] = []
    xg_available = 0
    lineups_available = 0
    statistics_available = 0
    approved = 0
    results: list[dict] = []

    league_acc: dict[str, dict] = defaultdict(
        lambda: {
            "fixtures": 0,
            "approved": 0,
            "warning": 0,
            "rejected": 0,
            "scores": [],
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

        scores.append(score)
        decision_counts[decision] += 1
        blocker_counts.update(blockers)
        warning_counts.update(warnings)
        if assessment.get("approved_for_training"):
            approved += 1

        xg_records = int(((coverage.get("xg") or {}).get("records", 0)) or 0)
        lineup_records = int(((coverage.get("lineups") or {}).get("records", 0)) or 0)
        statistic_records = int(((coverage.get("statistics") or {}).get("records", 0)) or 0)
        if xg_records > 0:
            xg_available += 1
        if lineup_records > 0:
            lineups_available += 1
        if statistic_records > 0:
            statistics_available += 1

        canonical = canonical_league(fixture.league_name)
        league_name = str(canonical.get("canonical_name") or fixture.league_name or "Unknown")
        bucket = league_acc[league_name]
        bucket["fixtures"] += 1
        bucket["scores"].append(score)
        bucket["xg_available"] += 1 if xg_records > 0 else 0
        if decision in {"approved_for_training", "warning", "rejected"}:
            bucket["approved" if decision == "approved_for_training" else decision] += 1

        results.append(
            {
                "sportmonks_fixture_id": fixture.sportmonks_id,
                "fixture_id": fixture.id,
                "league": league_name,
                "starts_at": fixture.starts_at.isoformat() if fixture.starts_at else None,
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
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

    def pct(value: int) -> float:
        return round((value / evaluated * 100.0), 1) if evaluated else 0.0

    by_league = []
    for league_name, bucket in league_acc.items():
        count = int(bucket["fixtures"])
        league_scores = list(bucket["scores"])
        by_league.append(
            {
                "league": league_name,
                "fixtures": count,
                "approved": int(bucket["approved"]),
                "warning": int(bucket["warning"]),
                "rejected": int(bucket["rejected"]),
                "approval_pct": round(bucket["approved"] / count * 100.0, 1) if count else 0.0,
                "xg_coverage_pct": round(bucket["xg_available"] / count * 100.0, 1) if count else 0.0,
                "average_quality_score": round(mean(league_scores), 1) if league_scores else 0.0,
            }
        )
    by_league.sort(key=lambda row: (-row["fixtures"], row["league"]))

    return {
        "status": "ok",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leagues": leagues or [],
        "normalized_league_keys": sorted(requested_keys),
        "limit": limit,
        "selected_fixtures": len(fixtures),
        "evaluated": evaluated,
        "summary": {
            "approved_for_training": int(decision_counts.get("approved_for_training", 0)),
            "warning": int(decision_counts.get("warning", 0)),
            "rejected": int(decision_counts.get("rejected", 0)),
            "approval_pct": pct(approved),
            "warning_pct": pct(int(decision_counts.get("warning", 0))),
            "rejection_pct": pct(int(decision_counts.get("rejected", 0))),
            "average_quality_score": round(mean(scores), 1) if scores else 0.0,
            "min_quality_score": round(min(scores), 1) if scores else 0.0,
            "max_quality_score": round(max(scores), 1) if scores else 0.0,
            "lineups_coverage_pct": pct(lineups_available),
            "statistics_coverage_pct": pct(statistics_available),
            "xg_coverage_pct": pct(xg_available),
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
            "training_scale_target_approval_pct": 90.0,
            "note": "Scale historical ingestion only after reviewing rejection causes and achieving stable structural quality across target leagues.",
        },
    }
