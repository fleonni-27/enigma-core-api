from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.database import engine

DB_QUERY_PLAN_AUDIT_VERSION = "db_query_plan_audit_v1"
J1_PREDICTION_WINDOW = "j1_45m_v1"
J1_LEDGER_SOURCE = "daily_prediction_runner_v1"
SAMPLE_FIXTURE_LIMIT = 25


def _as_json_plan(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list) and value:
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError("unexpected EXPLAIN JSON payload")
    return value


def _index_names(node: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        index_name = node.get("Index Name")
        if index_name:
            names.add(str(index_name))
        for value in node.values():
            names.update(_index_names(value))
    elif isinstance(node, list):
        for value in node:
            names.update(_index_names(value))
    return names


def _run_explain(
    connection: Connection,
    *,
    query_name: str,
    sql: str,
    params: dict[str, Any] | None = None,
    expanding: tuple[str, ...] = (),
) -> dict[str, Any]:
    statement = text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
    for name in expanding:
        statement = statement.bindparams(bindparam(name, expanding=True))
    payload = connection.execute(statement, params or {}).scalar_one()
    plan = _as_json_plan(payload)
    root = plan.get("Plan") or {}
    indexes = sorted(_index_names(root))
    return {
        "query_name": query_name,
        "planning_ms": float(plan.get("Planning Time") or 0.0),
        "execution_ms": float(plan.get("Execution Time") or 0.0),
        "total_cost": float(root.get("Total Cost") or 0.0),
        "plan_rows": int(root.get("Plan Rows") or 0),
        "indexes_used": indexes,
        "plan": plan,
    }


def _sample_context(connection: Connection) -> dict[str, Any]:
    fixture_ids = [
        int(row[0])
        for row in connection.execute(
            text(
                "SELECT id FROM fixtures "
                "ORDER BY starts_at DESC, id DESC LIMIT :limit"
            ),
            {"limit": SAMPLE_FIXTURE_LIMIT},
        ).all()
    ]
    if not fixture_ids:
        return {"fixture_ids": []}

    fixture_id = fixture_ids[0]
    snapshot_window = connection.execute(
        text(
            "SELECT snapshot_window FROM odds_snapshots "
            "WHERE fixture_id IN :fixture_ids AND snapshot_window IS NOT NULL "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1"
        ).bindparams(bindparam("fixture_ids", expanding=True)),
        {"fixture_ids": fixture_ids},
    ).scalar_one_or_none()
    if snapshot_window is None:
        snapshot_window = connection.execute(
            text(
                "SELECT snapshot_window FROM odds_snapshots "
                "WHERE snapshot_window IS NOT NULL "
                "ORDER BY fetched_at DESC, id DESC LIMIT 1"
            )
        ).scalar_one_or_none()

    fixture_row = connection.execute(
        text(
            "SELECT home_team, away_team, starts_at, sportmonks_id "
            "FROM fixtures WHERE id = :fixture_id"
        ),
        {"fixture_id": fixture_id},
    ).one()

    decision_sportmonks_id = connection.execute(
        text(
            "SELECT sportmonks_fixture_id FROM decision_records "
            "ORDER BY recorded_at DESC, id DESC LIMIT 1"
        )
    ).scalar_one_or_none()

    return {
        "fixture_ids": fixture_ids,
        "fixture_id": fixture_id,
        "snapshot_window": str(snapshot_window) if snapshot_window else None,
        "team": str(fixture_row.home_team),
        "cutoff": fixture_row.starts_at,
        "sportmonks_fixture_id": int(decision_sportmonks_id or fixture_row.sportmonks_id),
    }


def collect_hot_path_plans() -> dict[str, Any]:
    """Run bounded representative EXPLAIN ANALYZE probes against production data.

    The probes are read-only and intentionally limited to recent fixtures. They
    mirror the query shapes used by Operations V2, J1 selection/decision,
    inference training history and the forward-test ledger.
    """

    if engine.dialect.name != "postgresql":
        return {
            "status": "skipped",
            "version": DB_QUERY_PLAN_AUDIT_VERSION,
            "reason": "POSTGRESQL_REQUIRED",
            "plans": [],
        }

    plans: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    with engine.connect() as connection:
        context = _sample_context(connection)
        fixture_ids = context.get("fixture_ids") or []
        if not fixture_ids:
            return {
                "status": "skipped",
                "version": DB_QUERY_PLAN_AUDIT_VERSION,
                "reason": "NO_FIXTURES_AVAILABLE",
                "plans": [],
            }

        common = {"fixture_ids": fixture_ids}
        snapshot_window = context.get("snapshot_window")

        probes: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = [
            (
                "dashboard_odds_bulk",
                "SELECT fixture_id, count(id), max(fetched_at) "
                "FROM odds_snapshots WHERE fixture_id IN :fixture_ids "
                "GROUP BY fixture_id",
                common,
                ("fixture_ids",),
            ),
            (
                "j1_predictions_latest",
                "SELECT id, fixture_id, generated_at FROM predictions "
                "WHERE fixture_id IN :fixture_ids AND prediction_window = :prediction_window "
                "ORDER BY fixture_id ASC, generated_at DESC, id DESC",
                {**common, "prediction_window": J1_PREDICTION_WINDOW},
                ("fixture_ids",),
            ),
            (
                "j1_recorded_exclusion",
                "SELECT fixture_id, snapshot_window FROM decision_records "
                "WHERE fixture_id IN :fixture_ids AND source = :source",
                {**common, "source": J1_LEDGER_SOURCE},
                ("fixture_ids",),
            ),
            (
                "training_latest_snapshot",
                "SELECT id, fetched_at FROM fixture_data_snapshots "
                "WHERE fixture_id = :fixture_id "
                "ORDER BY fetched_at DESC, id DESC LIMIT 1",
                {"fixture_id": context["fixture_id"]},
                (),
            ),
            (
                "training_team_history",
                "SELECT id, starts_at FROM fixtures "
                "WHERE starts_at < :cutoff AND (home_team = :team OR away_team = :team) "
                "ORDER BY starts_at DESC, id DESC LIMIT 30",
                {"cutoff": context["cutoff"], "team": context["team"]},
                (),
            ),
            (
                "ledger_unsettled_by_start",
                "SELECT id, fixture_starts_at, recorded_at FROM decision_records "
                "WHERE settlement_status = 'UNSETTLED' AND fixture_starts_at <= now() "
                "ORDER BY recorded_at DESC, id DESC LIMIT 100",
                {},
                (),
            ),
            (
                "ledger_fixture_history",
                "SELECT id, recorded_at FROM decision_records "
                "WHERE sportmonks_fixture_id = :sportmonks_fixture_id "
                "ORDER BY recorded_at DESC, id DESC LIMIT 100",
                {"sportmonks_fixture_id": context["sportmonks_fixture_id"]},
                (),
            ),
        ]

        if snapshot_window:
            probes.extend(
                [
                    (
                        "dashboard_j1_context_latest",
                        "SELECT id, fixture_id, fetched_at FROM prematch_context_snapshots "
                        "WHERE fixture_id IN :fixture_ids AND snapshot_window = :snapshot_window "
                        "ORDER BY fixture_id ASC, fetched_at DESC, id DESC",
                        {**common, "snapshot_window": snapshot_window},
                        ("fixture_ids",),
                    ),
                    (
                        "dashboard_j1_decisions_latest",
                        "SELECT id, fixture_id, recorded_at FROM decision_records "
                        "WHERE fixture_id IN :fixture_ids AND snapshot_window = :snapshot_window "
                        "AND source = :source "
                        "ORDER BY fixture_id ASC, recorded_at DESC, id DESC",
                        {
                            **common,
                            "snapshot_window": snapshot_window,
                            "source": J1_LEDGER_SOURCE,
                        },
                        ("fixture_ids",),
                    ),
                    (
                        "decision_engine_j1_odds",
                        "SELECT id, fetched_at FROM odds_snapshots "
                        "WHERE fixture_id = :fixture_id AND snapshot_window = :snapshot_window "
                        "ORDER BY fetched_at DESC, id DESC",
                        {
                            "fixture_id": context["fixture_id"],
                            "snapshot_window": snapshot_window,
                        },
                        (),
                    ),
                ]
            )
        else:
            skipped.append(
                {
                    "query_name": "j1_window_queries",
                    "reason": "NO_SNAPSHOT_WINDOW_AVAILABLE",
                }
            )

        for query_name, sql, params, expanding in probes:
            try:
                plans.append(
                    _run_explain(
                        connection,
                        query_name=query_name,
                        sql=sql,
                        params=params,
                        expanding=expanding,
                    )
                )
            except Exception as exc:
                skipped.append(
                    {
                        "query_name": query_name,
                        "reason": exc.__class__.__name__,
                    }
                )

    return {
        "status": "ok",
        "version": DB_QUERY_PLAN_AUDIT_VERSION,
        "sample_fixture_count": len(fixture_ids),
        "plans": plans,
        "skipped": skipped,
    }


def persist_plan_audit(*, release_id: str, phase: str, audit: dict[str, Any]) -> int:
    if engine.dialect.name != "postgresql" or audit.get("status") != "ok":
        return 0
    if phase not in {"before", "after"}:
        raise ValueError("phase must be before or after")

    rows = []
    for item in audit.get("plans") or []:
        rows.append(
            {
                "release_id": release_id[:80],
                "phase": phase,
                "query_name": str(item.get("query_name") or "unknown")[:120],
                "planning_ms": Decimal(str(item.get("planning_ms") or 0.0)),
                "execution_ms": Decimal(str(item.get("execution_ms") or 0.0)),
                "total_cost": Decimal(str(item.get("total_cost") or 0.0)),
                "plan_rows": int(item.get("plan_rows") or 0),
                "indexes_used": json.dumps(item.get("indexes_used") or []),
                "plan_json": json.dumps(item.get("plan") or {}, default=str),
            }
        )

    if not rows:
        return 0

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO db_query_plan_audits "
                "(release_id, phase, query_name, planning_ms, execution_ms, total_cost, "
                "plan_rows, indexes_used, plan_json) VALUES "
                "(:release_id, :phase, :query_name, :planning_ms, :execution_ms, :total_cost, "
                ":plan_rows, :indexes_used, :plan_json)"
            ),
            rows,
        )
    return len(rows)


def summarize_comparison(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    before_by_name = {
        str(item.get("query_name")): item for item in (before or {}).get("plans", [])
    }
    comparisons = []
    for current in after.get("plans") or []:
        name = str(current.get("query_name"))
        previous = before_by_name.get(name)
        before_ms = float(previous.get("execution_ms")) if previous else None
        after_ms = float(current.get("execution_ms") or 0.0)
        delta_pct = None
        if before_ms and before_ms > 0:
            delta_pct = round(((after_ms - before_ms) / before_ms) * 100.0, 2)
        comparisons.append(
            {
                "query_name": name,
                "before_execution_ms": before_ms,
                "after_execution_ms": after_ms,
                "execution_delta_pct": delta_pct,
                "after_indexes_used": current.get("indexes_used") or [],
            }
        )
    return {
        "version": DB_QUERY_PLAN_AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "comparisons": comparisons,
    }
