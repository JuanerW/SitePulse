"""Add job owners, worker heartbeats, retry counts, and per-URL connection details.

Revision ID: 20261017_0003
Revises: 20260728_0002
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261017_0003"
down_revision: str | None = "20260728_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("owner_hash", sa.String(64), nullable=True))
    op.create_index("ix_jobs_owner_hash", "jobs", ["owner_hash"])
    op.add_column(
        "jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("check_results", sa.Column("final_url", sa.Text(), nullable=True))
    op.add_column("check_results", sa.Column("remote_ip", sa.String(45), nullable=True))
    op.add_column(
        "check_results",
        sa.Column("redirect_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("check_results", "redirect_count")
    op.drop_column("check_results", "remote_ip")
    op.drop_column("check_results", "final_url")
    op.drop_column("jobs", "retry_count")
    op.drop_column("jobs", "heartbeat_at")
    op.drop_index("ix_jobs_owner_hash", table_name="jobs")
    op.drop_column("jobs", "owner_hash")
