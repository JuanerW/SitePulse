"""Add upload idempotency fields.

Revision ID: 20260728_0002
Revises: 20260728_0001
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0002"
down_revision: str | None = "20260728_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("jobs", sa.Column("request_fingerprint", sa.String(64), nullable=True))
    op.create_index(
        "ix_jobs_idempotency_key",
        "jobs",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_idempotency_key", table_name="jobs")
    op.drop_column("jobs", "request_fingerprint")
    op.drop_column("jobs", "idempotency_key")
