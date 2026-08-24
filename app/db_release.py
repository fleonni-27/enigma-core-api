from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.database import engine
from app.db_query_plan_audit import (
    collect_hot_path_plans,
    persist_plan_audit,
    summarize_comparison,
)

DB_RELEASE_VERSION = "db_release_v1"
BASELINE_REVISION = "20260824_0001"
INDEX_REVISION = "20260824_0002"
ANALYZE_TABLES = (
    "fixtures",
    "odds_snapshots",
    "predictions",
    "fixture_data_snapshots",
    "prematch_context_snapshots",
    "decision_records",
)


def _alembic_config() -> Config:
    return Config("alembic.ini")


def _current_revision() -> str | None:
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one_or_none()


def _release_id() -> str:
    value = os.getenv("RENDER_GIT_COMMIT") or os.getenv("GITHUB_SHA")
    if value:
        return value[:80]
    return datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%SZ")


def _safe_collect() -> dict[str, Any]:
    try:
        return collect_hot_path_plans()
    except Exception as exc:
        return {
            "status": "failed",
            "error": exc.__class__.__name__,
            "plans": [],
        }


def _refresh_statistics() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if engine.dialect.name != "postgresql":
        return results
    for table in ANALYZE_TABLES:
        try:
            with engine.begin() as connection:
                connection.execute(text(f"ANALYZE {table}"))
            results.append({"table": table, "status": "ok"})
        except Exception as exc:
            results.append({"table": table, "status": "failed", "error": exc.__class__.__name__})
    return results


def run_release() -> dict[str, Any]:
    release_id = _release_id()
    initial_revision = _current_revision()
    config = _alembic_config()
    before: dict[str, Any] | None = None
    before_rows = 0

    # On the first managed migration deployment, install only the audit table,
    # capture the production plans, then add performance indexes. This preserves
    # a real before/after EXPLAIN ANALYZE comparison for the adoption release.
    if initial_revision is None:
        command.upgrade(config, BASELINE_REVISION)
        before = _safe_collect()
        before_rows = persist_plan_audit(
            release_id=release_id,
            phase="before",
            audit=before,
        )
    elif initial_revision == BASELINE_REVISION:
        before = _safe_collect()
        before_rows = persist_plan_audit(
            release_id=release_id,
            phase="before",
            audit=before,
        )

    command.upgrade(config, "head")
    statistics = _refresh_statistics()
    after = _safe_collect()
    after_rows = persist_plan_audit(
        release_id=release_id,
        phase="after",
        audit=after,
    )

    return {
        "status": "ok",
        "version": DB_RELEASE_VERSION,
        "release_id": release_id,
        "initial_revision": initial_revision,
        "final_revision": _current_revision(),
        "baseline_explain_captured": before is not None,
        "before_audit_rows": before_rows,
        "after_audit_rows": after_rows,
        "statistics": statistics,
        "comparison": summarize_comparison(before, after),
        "policy": {
            "schema_changes_managed_by_alembic": True,
            "production_indexes_created_concurrently": True,
            "explain_analyze_is_read_only": True,
            "before_after_plans_persisted": True,
            "migration_failure_blocks_release": True,
            "audit_probe_failure_does_not_rollback_successful_migration": True,
        },
    }


def main() -> None:
    result = run_release()
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
