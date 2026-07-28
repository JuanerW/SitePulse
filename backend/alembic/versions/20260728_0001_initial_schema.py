"""Create the initial SitePulse schema.

Revision ID: 20260728_0001
Revises:
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260728_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create a fresh schema or adopt the project's pre-Alembic schema."""

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    if "jobs" not in tables:
        op.create_table(
            "jobs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("total_urls", sa.Integer(), nullable=False),
            sa.Column("completed_urls", sa.Integer(), server_default="0", nullable=False),
            sa.Column("successful_urls", sa.Integer(), server_default="0", nullable=False),
            sa.Column("failed_urls", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        job_columns = {column["name"] for column in inspector.get_columns("jobs")}
        additions = {
            "completed_urls": sa.Column(
                "completed_urls", sa.Integer(), server_default="0", nullable=False
            ),
            "started_at": sa.Column(
                "started_at", sa.DateTime(timezone=True), nullable=True
            ),
            "finished_at": sa.Column(
                "finished_at", sa.DateTime(timezone=True), nullable=True
            ),
            "error": sa.Column("error", sa.Text(), nullable=True),
        }
        for name, column in additions.items():
            if name not in job_columns:
                op.add_column("jobs", column)

        op.execute(
            """
            UPDATE jobs
            SET completed_urls = total_urls,
                finished_at = COALESCE(finished_at, created_at)
            WHERE status = 'completed' AND completed_urls = 0
            """
        )

    if "check_results" not in tables:
        op.create_table(
            "check_results",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("response_time_ms", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_check_results_job_id"),
            "check_results",
            ["job_id"],
            unique=False,
        )
    else:
        op.execute(
            """
            UPDATE check_results
            SET status = CASE
                WHEN status = 'success' THEN 'healthy'
                WHEN status_code IN (401, 403) THEN 'blocked'
                WHEN status_code = 404 THEN 'not_found'
                WHEN status_code = 429 THEN 'rate_limited'
                WHEN status_code BETWEEN 500 AND 599 THEN 'server_error'
                WHEN error = 'Request timed out' THEN 'timeout'
                WHEN error LIKE 'Network error:%' THEN 'network_error'
                ELSE 'client_error'
            END
            WHERE status IN ('success', 'failed')
            """
        )


def downgrade() -> None:
    """Remove all SitePulse tables from a database created by this revision."""

    op.drop_index(op.f("ix_check_results_job_id"), table_name="check_results")
    op.drop_table("check_results")
    op.drop_table("jobs")
