"""Add composite indexes for Enigma Core hot paths.

Revision ID: 20260824_0002
Revises: 20260824_0001
Create Date: 2026-08-24

These indexes match the current J1, dashboard, inference/training and ledger
query shapes. PostgreSQL builds them CONCURRENTLY to avoid blocking production
writes for the duration of a full index build.
"""

from alembic import op

revision = "20260824_0002"
down_revision = "20260824_0001"
branch_labels = None
depends_on = None

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


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Production is PostgreSQL. Tests may use SQLite; advancing the revision
        # there is sufficient because these indexes are PostgreSQL scale work.
        return

    context = op.get_context()
    with context.autocommit_block():
        for name, table, columns in INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} ({columns})"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    context = op.get_context()
    with context.autocommit_block():
        for name, _table, _columns in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
