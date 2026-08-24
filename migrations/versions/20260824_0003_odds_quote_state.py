"""Track quote observation state for movement-preserving dedupe.

Revision ID: 20260824_0003
Revises: 20260824_0002
Create Date: 2026-08-24

Repeated observations of the same price no longer need one physical row each.
`first_seen_at` marks the beginning of a price state, while the existing
`fetched_at` column continues to mean latest observation for all current readers.
`observation_count` records how many identical observations were collapsed.

The production release path is resumable. If a previous attempt committed the
columns but was interrupted during index creation, the next attempt detects the
existing columns and continues safely. PostgreSQL index creation uses the same
bounded regular-build policy as revision 0002 to avoid an indefinite concurrent
index wait.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "20260824_0003"
down_revision = "20260824_0002"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_odds_quote_stream_latest"
INDEX_COLUMNS = (
    "fixture_id, snapshot_window, bookmaker, market, selection, source, "
    "fetched_at DESC, id DESC"
)
LOCK_TIMEOUT = "30s"
STATEMENT_TIMEOUT = "5min"


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
    inspector = sa.inspect(bind)
    # The migration chain CI intentionally runs against an empty SQLite DB: the
    # legacy application tables predate Alembic and are not created by revisions
    # 0001/0002. Production is PostgreSQL and must already have odds_snapshots.
    if not inspector.has_table("odds_snapshots"):
        if bind.dialect.name == "postgresql":
            raise RuntimeError("odds_snapshots table is required before revision 0003")
        return

    existing_columns = {column["name"] for column in inspector.get_columns("odds_snapshots")}
    if "first_seen_at" not in existing_columns:
        op.add_column(
            "odds_snapshots",
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "observation_count" not in existing_columns:
        op.add_column(
            "odds_snapshots",
            sa.Column(
                "observation_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )

    # Existing rows were one observation per physical row, so fetched_at is also
    # their first-seen time. This statement is idempotent after an interrupted run.
    op.execute(
        "UPDATE odds_snapshots SET first_seen_at = fetched_at "
        "WHERE first_seen_at IS NULL"
    )

    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            op.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            try:
                state = _index_valid(bind, INDEX_NAME)
                if state is False:
                    print(f"db_release index={INDEX_NAME} status=drop_invalid", flush=True)
                    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
                if state is not True:
                    print(f"db_release index={INDEX_NAME} status=building", flush=True)
                    op.execute(
                        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
                        f"ON odds_snapshots ({INDEX_COLUMNS})"
                    )
                    print(f"db_release index={INDEX_NAME} status=ready", flush=True)
                else:
                    print(f"db_release index={INDEX_NAME} status=already_valid", flush=True)
            finally:
                op.execute("RESET statement_timeout")
                op.execute("RESET lock_timeout")
    else:
        index_names = {index["name"] for index in inspector.get_indexes("odds_snapshots")}
        if INDEX_NAME not in index_names:
            op.create_index(
                INDEX_NAME,
                "odds_snapshots",
                [
                    "fixture_id",
                    "snapshot_window",
                    "bookmaker",
                    "market",
                    "selection",
                    "source",
                    "fetched_at",
                    "id",
                ],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("odds_snapshots"):
        return

    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            try:
                op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
            finally:
                op.execute("RESET lock_timeout")
    else:
        index_names = {index["name"] for index in inspector.get_indexes("odds_snapshots")}
        if INDEX_NAME in index_names:
            op.drop_index(INDEX_NAME, table_name="odds_snapshots")

    existing_columns = {column["name"] for column in sa.inspect(bind).get_columns("odds_snapshots")}
    if "observation_count" in existing_columns:
        op.drop_column("odds_snapshots", "observation_count")
    if "first_seen_at" in existing_columns:
        op.drop_column("odds_snapshots", "first_seen_at")
