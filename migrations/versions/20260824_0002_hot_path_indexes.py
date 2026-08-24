"""Add composite indexes for Enigma Core hot paths.

Revision ID: 20260824_0002
Revises: 20260824_0001
Create Date: 2026-08-24

The first production adoption showed that CREATE INDEX CONCURRENTLY could wait
indefinitely for old transactions while the Render service remained healthy.
This revision is still safe to change because production never advanced beyond
0001. Index creation is now resumable: valid indexes are skipped, invalid
artifacts left by interrupted concurrent builds are dropped, and regular index
builds use bounded lock/statement timeouts in autocommit mode. Each completed
index survives a later interruption, so the next attempt resumes instead of
starting the whole set from zero.
"""

from alembic import op
from sqlalchemy import text

revision = "20260824_0002"
down_revision = "20260824_0001"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "30s"
STATEMENT_TIMEOUT = "5min"

INDEXES = (
    (
        "ix_odds_fixture_window_fetched_id",
        "odds_snapshots",
        "fixture_id, snapshot_window, fetched_at DESC, id DESC",
    ),
    (
        "ix_prediction_fixture_window_generated_id",
        "predictions",
        "fixture_id, prediction_window, generated_at DESC, id DESC",
    ),
    (
        "ix_context_fixture_window_fetched_id",
        "prematch_context_snapshots",
        "fixture_id, snapshot_window, fetched_at DESC, id DESC",
    ),
    (
        "ix_decision_fixture_source_window_recorded_id",
        "decision_records",
        "fixture_id, source, snapshot_window, recorded_at DESC, id DESC",
    ),
    (
        "ix_fixture_data_fixture_fetched_id",
        "fixture_data_snapshots",
        "fixture_id, fetched_at DESC, id DESC",
    ),
    (
        "ix_fixture_home_starts_id",
        "fixtures",
        "home_team, starts_at DESC, id DESC",
    ),
    (
        "ix_fixture_away_starts_id",
        "fixtures",
        "away_team, starts_at DESC, id DESC",
    ),
    (
        "ix_decision_settlement_starts_recorded_id",
        "decision_records",
        "settlement_status, fixture_starts_at, recorded_at DESC, id DESC",
    ),
    (
        "ix_decision_sportmonks_recorded_id",
        "decision_records",
        "sportmonks_fixture_id, recorded_at DESC, id DESC",
    ),
)


def _index_valid(bind, name: str) -> bool | None:
    row = bind.execute(
        text(
            """
            SELECT i.indisvalid
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'i'
              AND c.relname = :name
              AND n.nspname = current_schema()
            LIMIT 1
            """
        ),
        {"name": name},
    ).first()
    if row is None:
        return None
    return bool(row[0])


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Production is PostgreSQL. Tests may use SQLite; advancing the revision
        # there is sufficient because these indexes are PostgreSQL scale work.
        return

    context = op.get_context()
    with context.autocommit_block():
        op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        op.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        try:
            for name, table, columns in INDEXES:
                state = _index_valid(bind, name)
                if state is True:
                    print(f"db_release index={name} status=already_valid", flush=True)
                    continue
                if state is False:
                    print(f"db_release index={name} status=drop_invalid", flush=True)
                    op.execute(f"DROP INDEX IF EXISTS {name}")
                print(f"db_release index={name} status=building", flush=True)
                op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")
                print(f"db_release index={name} status=ready", flush=True)
        finally:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    context = op.get_context()
    with context.autocommit_block():
        op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        try:
            for name, _table, _columns in reversed(INDEXES):
                op.execute(f"DROP INDEX IF EXISTS {name}")
        finally:
            op.execute("RESET lock_timeout")
