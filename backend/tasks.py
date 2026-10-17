import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, delete, or_, select, text, update
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
HEARTBEAT_SECONDS = 5
# A processing job whose heartbeat is older than this has lost its worker.
STALE_AFTER_SECONDS = 30
JOB_RETENTION_HOURS = int(os.getenv("JOB_RETENTION_HOURS", "72"))
# Jobs created without an owner token (plain curl, or rows from before
# ownership existed) cannot be managed by anyone, so they go sooner.
ORPHAN_RETENTION_HOURS = 1


async def beat_heartbeat(session_factory: async_sessionmaker, job_id: uuid.UUID) -> None:
    """
    Mark the job alive every few seconds while it runs.

    Writing only when a URL finishes would look dead whenever ten slow URLs
    are in flight together. This uses its own session because the main one
    is busy with result rows.
    """

    while True:
        try:
            async with session_factory() as session:
                await session.execute(
                    update(models.Job)
                    .where(models.Job.id == job_id)
                    .values(heartbeat_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception:
            # One failed write must not end the loop; a missing heartbeat
            # would make a healthy job look dead and get it requeued.
            logger.exception("heartbeat_write_failed")
        await asyncio.sleep(HEARTBEAT_SECONDS)


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
                # A duplicate delivery while another worker runs the job. It is
                # safe to drop: if that worker dies, its heartbeat stops and
                # reap_stale_jobs queues the job again.
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
            job.heartbeat_at = datetime.now(timezone.utc)
            job.error = None
            await session.commit()
            heartbeat = asyncio.create_task(beat_heartbeat(session_factory, job_id))

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
                    record.final_url = result.final_url
                    record.remote_ip = result.remote_ip
                    record.redirect_count = result.redirect_count

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

            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
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


async def requeue_stale_jobs() -> list[tuple[uuid.UUID, str | None]]:
    """Find processing jobs whose worker stopped sending heartbeats."""

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
    try:
        async with session_factory() as session:
            stale = (
                await session.execute(
                    select(models.Job.id, models.Job.request_id).where(
                        models.Job.status == "processing",
                        or_(
                            models.Job.heartbeat_at.is_(None),
                            models.Job.heartbeat_at < cutoff,
                        ),
                    )
                )
            ).all()
            if stale:
                # Count the requeue as a heartbeat, so a job waiting behind busy
                # workers is requeued at most once per stale window, not on
                # every 15 s reaper pass.
                await session.execute(
                    update(models.Job)
                    .where(models.Job.id.in_([row.id for row in stale]))
                    .values(heartbeat_at=datetime.now(timezone.utc))
                )
                await session.commit()
            return [(row.id, row.request_id) for row in stale]
    finally:
        await engine.dispose()


async def delete_expired_jobs() -> int:
    """Apply the retention window on a timer, not only when traffic arrives."""

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            result = await session.execute(
                delete(models.Job).where(
                    or_(
                        models.Job.created_at < now - timedelta(hours=JOB_RETENTION_HOURS),
                        and_(
                            models.Job.owner_hash.is_(None),
                            models.Job.created_at
                            < now - timedelta(hours=ORPHAN_RETENTION_HOURS),
                            models.Job.status.notin_(["queued", "processing"]),
                        ),
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)
    finally:
        await engine.dispose()


@celery_app.task(name="sitepulse.reap_stale_jobs")
def reap_stale_jobs() -> None:
    """
    Requeue jobs whose worker died mid-run.

    Redis would redeliver the unacknowledged message on its own, but only after
    the visibility timeout plus kombu's ~100 s scan interval. The heartbeat
    makes a dead worker visible in about 30 s. Requeueing is safe: the advisory
    lock stops a second copy from running while the first is alive, and only
    rows still marked queued are checked.
    """

    for job_id, request_id in asyncio.run(requeue_stale_jobs()):
        with correlation_context(request_id=request_id, job_id=str(job_id)):
            logger.warning("stale_job_requeued")
        check_job.delay(str(job_id), request_id)


@celery_app.task(name="sitepulse.cleanup_jobs")
def cleanup_jobs() -> None:
    deleted = asyncio.run(delete_expired_jobs())
    if deleted:
        logger.info("expired_jobs_deleted", extra={"job_count": deleted})
