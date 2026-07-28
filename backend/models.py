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

    job: Mapped[Job] = relationship(back_populates="results")
