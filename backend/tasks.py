import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
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


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://sitepulse:sitepulse_dev@localhost:5432/sitepulse",
)


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
            query = (
                select(models.Job)
                .options(selectinload(models.Job.results))
                .where(models.Job.id == job_id)
            )
            job = await session.scalar(query)

            if job is None:
                # A message may reference a deleted job; no retry is needed.
                return
            if job.status in {"completed", "cancelled"}:
                # Keep redelivery idempotent by skipping terminal jobs.
                return

            job.status = "processing"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.error = None
            await session.commit()

            # Only process queued rows so committed results survive worker crashes.
            pending_records = [
                record for record in job.results if record.status == "queued"
            ]
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

            await session.refresh(job, attribute_names=["status"])
            if job.status != "cancelled":
                job.status = "completed"
                job.finished_at = datetime.now(timezone.utc)
                await session.commit()
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
    max_retries=2,
    default_retry_delay=5,
)
def check_job(self, job_id: str) -> None:
    """
    Synchronous Celery entry point.

    Celery runs as a regular synchronous process on Windows; the HTTP and database work
    still uses asyncio and is driven by asyncio.run().
    """

    parsed_job_id = uuid.UUID(job_id)

    try:
        asyncio.run(process_job(parsed_job_id))
    except Exception as exc:
        asyncio.run(mark_job_failed(parsed_job_id, str(exc)))
        # self.retry requeues the message; it fails permanently after max_retries.
        raise self.retry(exc=exc)
