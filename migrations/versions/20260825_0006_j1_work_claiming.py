"""Add J1 work claiming queue.

Revision ID: 20260825_0006
Revises: 20260824_0005
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa

revision = "20260825_0006"
down_revision = "20260824_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "j1_work_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("fixture_id", sa.BigInteger(), sa.ForeignKey("fixtures.id"), nullable=False),
        sa.Column("sportmonks_fixture_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_window", sa.String(length=30), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kickoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.String(length=160), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_status", sa.String(length=60), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("fixture_id", "snapshot_window", name="uq_j1_work_fixture_window"),
    )
    op.create_index("ix_j1_work_fixture_id", "j1_work_items", ["fixture_id"], unique=False)
    op.create_index("ix_j1_work_sportmonks_fixture_id", "j1_work_items", ["sportmonks_fixture_id"], unique=False)
    op.create_index("ix_j1_work_snapshot_window", "j1_work_items", ["snapshot_window"], unique=False)
    op.create_index("ix_j1_work_due_at", "j1_work_items", ["due_at"], unique=False)
    op.create_index("ix_j1_work_kickoff_at", "j1_work_items", ["kickoff_at"], unique=False)
    op.create_index("ix_j1_work_status", "j1_work_items", ["status"], unique=False)
    op.create_index("ix_j1_work_available_at", "j1_work_items", ["available_at"], unique=False)
    op.create_index("ix_j1_work_claimed_by", "j1_work_items", ["claimed_by"], unique=False)
    op.create_index("ix_j1_work_claim_token", "j1_work_items", ["claim_token"], unique=False)
    op.create_index("ix_j1_work_lease_expires_at", "j1_work_items", ["lease_expires_at"], unique=False)
    op.create_index("ix_j1_work_created_at", "j1_work_items", ["created_at"], unique=False)
    op.create_index("ix_j1_work_updated_at", "j1_work_items", ["updated_at"], unique=False)
    op.create_index(
        "ix_j1_work_claim_scan",
        "j1_work_items",
        ["status", "available_at", "lease_expires_at", "kickoff_at", "due_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_j1_work_claim_scan", table_name="j1_work_items")
    op.drop_index("ix_j1_work_updated_at", table_name="j1_work_items")
    op.drop_index("ix_j1_work_created_at", table_name="j1_work_items")
    op.drop_index("ix_j1_work_lease_expires_at", table_name="j1_work_items")
    op.drop_index("ix_j1_work_claim_token", table_name="j1_work_items")
    op.drop_index("ix_j1_work_claimed_by", table_name="j1_work_items")
    op.drop_index("ix_j1_work_available_at", table_name="j1_work_items")
    op.drop_index("ix_j1_work_status", table_name="j1_work_items")
    op.drop_index("ix_j1_work_kickoff_at", table_name="j1_work_items")
    op.drop_index("ix_j1_work_due_at", table_name="j1_work_items")
    op.drop_index("ix_j1_work_snapshot_window", table_name="j1_work_items")
    op.drop_index("ix_j1_work_sportmonks_fixture_id", table_name="j1_work_items")
    op.drop_index("ix_j1_work_fixture_id", table_name="j1_work_items")
    op.drop_table("j1_work_items")
