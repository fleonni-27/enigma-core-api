"""Add persisted dashboard enrichment cache.

Revision ID: 20260827_0007
Revises: 20260825_0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260827_0007"
down_revision = "20260825_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_enrichment_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("fixture_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.String(length=80), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["fixture_id"], ["fixtures.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fixture_id", name="uq_dashboard_enrichment_fixture"),
    )
    op.create_index(
        "ix_dashboard_enrichment_fixture_generated",
        "dashboard_enrichment_snapshots",
        ["fixture_id", "generated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_enrichment_fixture_generated", table_name="dashboard_enrichment_snapshots")
    op.drop_table("dashboard_enrichment_snapshots")
