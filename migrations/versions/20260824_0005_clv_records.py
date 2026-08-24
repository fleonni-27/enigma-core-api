"""Persist immutable CLV records.

Revision ID: 20260824_0005
Revises: 20260824_0004
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa

revision = "20260824_0005"
down_revision = "20260824_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clv_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("decision_record_id", sa.BigInteger(), sa.ForeignKey("decision_records.id"), nullable=False),
        sa.Column("fixture_id", sa.BigInteger(), sa.ForeignKey("fixtures.id"), nullable=False),
        sa.Column("sportmonks_fixture_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(length=12), nullable=False),
        sa.Column("selection", sa.String(length=2), nullable=False),
        sa.Column("bookmaker", sa.String(length=120), nullable=False),
        sa.Column("market_name", sa.String(length=120), nullable=False),
        sa.Column("decision_snapshot_window", sa.String(length=30), nullable=False),
        sa.Column("closing_snapshot_window", sa.String(length=30), nullable=False),
        sa.Column("decision_odd", sa.Numeric(10, 4), nullable=False),
        sa.Column("closing_odd", sa.Numeric(10, 4), nullable=False),
        sa.Column("decision_no_vig_probability", sa.Numeric(10, 6), nullable=True),
        sa.Column("closing_no_vig_probability", sa.Numeric(10, 6), nullable=False),
        sa.Column("clv_odds_decimal", sa.Numeric(12, 6), nullable=False),
        sa.Column("clv_odds_pct", sa.Numeric(10, 3), nullable=False),
        sa.Column("clv_probability_points", sa.Numeric(10, 6), nullable=True),
        sa.Column("clv_probability_pp", sa.Numeric(10, 3), nullable=True),
        sa.Column("calibrated_confidence", sa.Numeric(10, 6), nullable=True),
        sa.Column("model_edge_vs_closing", sa.Numeric(10, 6), nullable=True),
        sa.Column("closing_quote_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("decision_record_id", name="uq_clv_records_decision_record_id"),
    )
    op.create_index("ix_clv_records_decision_record_id", "clv_records", ["decision_record_id"], unique=True)
    op.create_index("ix_clv_records_fixture_id", "clv_records", ["fixture_id"], unique=False)
    op.create_index("ix_clv_records_sportmonks_fixture_id", "clv_records", ["sportmonks_fixture_id"], unique=False)
    op.create_index("ix_clv_records_decision", "clv_records", ["decision"], unique=False)
    op.create_index("ix_clv_records_bookmaker", "clv_records", ["bookmaker"], unique=False)
    op.create_index("ix_clv_records_decision_window", "clv_records", ["decision_snapshot_window"], unique=False)
    op.create_index("ix_clv_records_closing_window", "clv_records", ["closing_snapshot_window"], unique=False)
    op.create_index("ix_clv_records_closing_quote_at", "clv_records", ["closing_quote_fetched_at"], unique=False)
    op.create_index("ix_clv_records_finalized_at", "clv_records", ["finalized_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_clv_records_finalized_at", table_name="clv_records")
    op.drop_index("ix_clv_records_closing_quote_at", table_name="clv_records")
    op.drop_index("ix_clv_records_closing_window", table_name="clv_records")
    op.drop_index("ix_clv_records_decision_window", table_name="clv_records")
    op.drop_index("ix_clv_records_bookmaker", table_name="clv_records")
    op.drop_index("ix_clv_records_decision", table_name="clv_records")
    op.drop_index("ix_clv_records_sportmonks_fixture_id", table_name="clv_records")
    op.drop_index("ix_clv_records_fixture_id", table_name="clv_records")
    op.drop_index("ix_clv_records_decision_record_id", table_name="clv_records")
    op.drop_table("clv_records")
