"""Persist pipeline performance samples for percentile observability.

Revision ID: 20260824_0004
Revises: 20260824_0003
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260824_0004"
down_revision = "20260824_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_performance_samples",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pipeline", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cycle_seconds", sa.Float(), nullable=True),
        sa.Column("upstream_seconds", sa.Float(), nullable=True),
        sa.Column("dataset_build_seconds", sa.Float(), nullable=True),
        sa.Column("fit_seconds", sa.Float(), nullable=True),
        sa.Column("selected_fixtures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("logical_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limited_responses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index(
        "ix_pipeline_perf_pipeline_observed",
        "pipeline_performance_samples",
        ["pipeline", "observed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_perf_status_observed",
        "pipeline_performance_samples",
        ["status", "observed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_pipeline_perf_run_id",
        "pipeline_performance_samples",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_perf_run_id", table_name="pipeline_performance_samples")
    op.drop_index("ix_pipeline_perf_status_observed", table_name="pipeline_performance_samples")
    op.drop_index("ix_pipeline_perf_pipeline_observed", table_name="pipeline_performance_samples")
    op.drop_table("pipeline_performance_samples")
