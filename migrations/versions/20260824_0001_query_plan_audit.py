"""Create persistent query-plan audit storage.

Revision ID: 20260824_0001
Revises: None
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260824_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "db_query_plan_audits",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("release_id", sa.String(length=80), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("query_name", sa.String(length=120), nullable=False),
        sa.Column("planning_ms", sa.Numeric(14, 6), nullable=True),
        sa.Column("execution_ms", sa.Numeric(14, 6), nullable=True),
        sa.Column("total_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("plan_rows", sa.BigInteger(), nullable=True),
        sa.Column("indexes_used", sa.Text(), nullable=True),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_db_query_plan_audits_release_phase",
        "db_query_plan_audits",
        ["release_id", "phase", "query_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_db_query_plan_audits_release_phase", table_name="db_query_plan_audits")
    op.drop_table("db_query_plan_audits")
