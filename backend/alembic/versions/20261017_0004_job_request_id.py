"""Store the request ID that created a job, so requeued work keeps it.

Revision ID: 20261017_0004
Revises: 20261017_0003
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261017_0004"
down_revision: str | None = "20261017_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("request_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "request_id")
