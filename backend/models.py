import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Job(Base):
    """One CSV upload corresponds to one check job."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    filename: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    request_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # SHA-256 of the browser's owner token. Only the hash is stored, so the
    # database alone cannot be used to act as a visitor.
    owner_hash: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="queued")
    total_urls: Mapped[int] = mapped_column(Integer)
    completed_urls: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    successful_urls: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    failed_urls: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Updated by the worker after every URL; a processing job whose heartbeat
    # stops moving has lost its worker and is waiting for redelivery.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # The X-Request-ID of the upload; reused when the reaper requeues the job
    # so one grep still finds every log line for it.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list["CheckResultRecord"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )


class CheckResultRecord(Base):
    """The check result for one URL in a job."""

    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    redirect_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )

    job: Mapped[Job] = relationship(back_populates="results")
