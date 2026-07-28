import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

import models
from celery_app import celery_app
from checker import (
    MAX_CONCURRENT_CHECKS,
    REQUEST_TIMEOUT_SECONDS,
    WebsiteCheck,
    check_one_website,
)
from observability import configure_logging, correlation_context


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://sitepulse:sitepulse_dev@localhost:5432/sitepulse",
)
configure_logging()
logger = logging.getLogger("sitepulse.worker")


async def process_job(job_id: uuid.UUID) -> None:
    """
    Run one complete background check job.

    A Celery worker is a separate process, so it creates its own database engine.
    Dispose the engine after the task to avoid sharing connections across asyncio event loops.
    """

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            lock_acquired = bool(
                await session.scalar(
                    text(
                        "SELECT pg_try_advisory_lock("
                        "hashtextextended(CAST(:job_id AS text), 0))"
                    ),
                    {"job_id": str(job_id)},
                )
            )
            if not lock_acquired:
                logger.info("job_skipped_already_running")
                return

            query = (
                select(models.Job)
                .options(selectinload(models.Job.results))
                .where(models.Job.id == job_id)
            )
            job = await session.scalar(query)

            if job is None:
                # A message may reference a deleted job; no retry is needed.
                logger.warning("job_not_found")
                return
            if job.status in {"completed", "cancelled"}:
                # Keep redelivery idempotent by skipping terminal jobs.
                logger.info("job_skipped_terminal", extra={"job_status": job.status})
                return

            job.status = "processing"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.error = None
            await session.commit()

            # Only process queued rows so committed results survive worker crashes.
            pending_records = [
                record for record in job.results if record.status == "queued"
            ]
            logger.info(
                "job_processing_started",
                extra={"url_count": len(pending_records)},
            )
            records_by_id = {record.id: record for record in pending_records}
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={
                    "User-Agent": "SitePulse/0.1 (educational status monitor)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            ) as client:

                async def check_record(
                    record: models.CheckResultRecord,
                ) -> tuple[int, WebsiteCheck]:
                    result = await check_one_website(client, semaphore, record.url)
                    return record.id, result

                checks = [
                    asyncio.create_task(check_record(record))
                    for record in pending_records
                ]

                # as_completed lets us update progress as soon as each URL finishes,
                # without waiting for the entire batch.
                for completed_check in asyncio.as_completed(checks):
                    record_id, result = await completed_check

                    # Cancellation comes from another API session, so refresh to see it.
                    await session.refresh(job, attribute_names=["status"])
                    if job.status == "cancelled":
                        for check in checks:
                            if not check.done():
                                check.cancel()
                        await asyncio.gather(*checks, return_exceptions=True)
                        break

                    record = records_by_id[record_id]
                    record.status = result.status
                    record.status_code = result.status_code
                    record.response_time_ms = result.response_time_ms
                    record.title = result.title
                    record.error = result.error

                    job.completed_urls += 1
                    if result.status == "healthy":
                        job.successful_urls += 1
                    else:
                        job.failed_urls += 1

                    # Commit after each URL so React can display live progress.
                    await session.commit()
                    logger.info(
                        "url_check_completed",
                        extra={
                            "result_id": record.id,
                            "result_status": result.status,
                            "status_code": result.status_code,
                            "duration_ms": result.response_time_ms,
                            "completed_urls": job.completed_urls,
                            "total_urls": job.total_urls,
                        },
                    )

            await session.refresh(job, attribute_names=["status"])
            if job.status != "cancelled":
                job.status = "completed"
                job.finished_at = datetime.now(timezone.utc)
                await session.commit()
                logger.info(
                    "job_completed",
                    extra={
                        "successful_urls": job.successful_urls,
                        "failed_urls": job.failed_urls,
                    },
                )
    finally:
        await engine.dispose()


async def mark_job_failed(job_id: uuid.UUID, message: str) -> None:
    """Record a job-level error for the API and frontend."""

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            job = await session.get(models.Job, job_id)
            if job is not None and job.status != "completed":
                job.status = "failed"
                job.error = message[:1000]
                await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True,
    name="sitepulse.check_job",
    max_retries=4,
    default_retry_delay=5,
)
def check_job(self, job_id: str, request_id: str | None = None) -> None:
    """
    Synchronous Celery entry point.

    Celery runs as a regular synchronous process on Windows; the HTTP and database work
    still uses asyncio and is driven by asyncio.run().
    """

    parsed_job_id = uuid.UUID(job_id)

    with correlation_context(request_id=request_id, job_id=job_id):
        logger.info(
            "celery_task_received",
            extra={"celery_task_id": self.request.id},
        )
        try:
            asyncio.run(process_job(parsed_job_id))
        except (DBAPIError, OSError) as exc:
            logger.exception(
                "celery_task_transient_failure",
                extra={"retry_number": self.request.retries},
            )
            if self.request.retries >= self.max_retries:
                asyncio.run(mark_job_failed(parsed_job_id, str(exc)))
                raise
            # Exponential backoff: 5, 10, 20, then 40 seconds.
            raise self.retry(
                exc=exc,
                countdown=min(60, 5 * (2**self.request.retries)),
            )
        except Exception as exc:
            logger.exception("celery_task_permanent_failure")
            asyncio.run(mark_job_failed(parsed_job_id, str(exc)))
            raise
