"""Track quote observation state for movement-preserving dedupe.

Revision ID: 20260824_0003
Revises: 20260824_0002
Create Date: 2026-08-24

Repeated observations of the same price no longer need one physical row each.
`fetched_at` remains the first-seen timestamp of a price state, while
`last_seen_at` and `observation_count` record continued observation. A composite
stream index supports loading the latest state for one fixture/window/market.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260824_0003"
down_revision = "20260824_0002"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_odds_quote_stream_latest"
INDEX_COLUMNS = (
    "fixture_id, snapshot_window, bookmaker, market, selection, source, "
    "fetched_at DESC, id DESC"
)


def upgrade() -> None:
    op.add_column(
        "odds_snapshots",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "odds_snapshots",
        sa.Column(
            "observation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                f"ON odds_snapshots ({INDEX_COLUMNS})"
            )
    else:
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
    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    else:
        op.drop_index(INDEX_NAME, table_name="odds_snapshots")

    op.drop_column("odds_snapshots", "observation_count")
    op.drop_column("odds_snapshots", "last_seen_at")
