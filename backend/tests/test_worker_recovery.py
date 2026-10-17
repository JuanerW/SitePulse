"""Crash recovery: duplicates never run twice, dead workers get their jobs back."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import models
import tasks


@pytest.mark.anyio
async def test_duplicate_delivery_is_skipped_while_job_is_locked() -> None:
    engine = create_async_engine(tasks.DATABASE_URL)
    job_id = uuid.uuid4()
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_lock(hashtextextended(CAST(:job_id AS text), 0))"),
                {"job_id": str(job_id)},
            )
            # The "first worker" holds the lock; the copy must not run.
            with patch.object(tasks, "check_one_website") as check:
                await tasks.process_job(job_id)
            check.assert_not_called()
    finally:
        await engine.dispose()


async def _insert_job(**fields) -> uuid.UUID:  # noqa: ANN003
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_async_engine(tasks.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            job = models.Job(filename="t.csv", total_urls=1, **fields)
            session.add(job)
            await session.commit()
            return job.id
    finally:
        await engine.dispose()


async def _job_exists(job_id: uuid.UUID) -> bool:
    engine = create_async_engine(tasks.DATABASE_URL)
    try:
        async with engine.connect() as connection:
            found = await connection.scalar(
                text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": job_id}
            )
            return bool(found)
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_reaper_finds_processing_jobs_with_a_stale_heartbeat() -> None:
    now = datetime.now(timezone.utc)
    stale = await _insert_job(status="processing", heartbeat_at=now - timedelta(seconds=90))
    alive = await _insert_job(status="processing", heartbeat_at=now)
    done = await _insert_job(status="completed", heartbeat_at=now - timedelta(hours=1))

    found = [job_id for job_id, _ in await tasks.requeue_stale_jobs()]

    assert stale in found
    assert alive not in found and done not in found
    # The requeue counts as a heartbeat: the next pass leaves it alone.
    again = [job_id for job_id, _ in await tasks.requeue_stale_jobs()]
    assert stale not in again


@pytest.mark.anyio
async def test_cleanup_drops_expired_and_orphaned_jobs() -> None:
    now = datetime.now(timezone.utc)
    expired = await _insert_job(
        status="completed", owner_hash="a" * 64, created_at=now - timedelta(days=4)
    )
    orphan = await _insert_job(status="completed", created_at=now - timedelta(hours=2))
    fresh = await _insert_job(status="completed", owner_hash="b" * 64)

    await tasks.delete_expired_jobs()

    assert not await _job_exists(expired)
    assert not await _job_exists(orphan)
    assert await _job_exists(fresh)


@pytest.mark.anyio
async def test_resumed_job_only_checks_rows_still_queued() -> None:
    """After a crash, URLs saved before the crash are not checked again."""

    engine = create_async_engine(tasks.DATABASE_URL)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    job = models.Job(
        filename="resume.csv",
        status="processing",  # left behind by the dead worker
        total_urls=3,
        completed_urls=1,
        successful_urls=1,
        failed_urls=0,
        results=[
            models.CheckResultRecord(url="https://done.example", status="healthy", status_code=200),
            models.CheckResultRecord(url="https://a.example", status="queued"),
            models.CheckResultRecord(url="https://b.example", status="queued"),
        ],
    )
    async with session_factory() as session:
        session.add(job)
        await session.commit()

    checked: list[str] = []

    async def fake_check(_client, _semaphore, url):  # noqa: ANN001
        checked.append(url)
        await asyncio.sleep(0)
        return tasks.WebsiteCheck(url=url, status="healthy", status_code=200)

    try:
        with patch.object(tasks, "check_one_website", side_effect=fake_check):
            await tasks.process_job(job.id)

        async with session_factory() as session:
            saved = await session.get(models.Job, job.id)
            assert saved is not None
            assert sorted(checked) == ["https://a.example", "https://b.example"]
            assert (saved.status, saved.completed_urls, saved.successful_urls) == (
                "completed",
                3,
                3,
            )
            await session.delete(saved)
            await session.commit()
    finally:
        await engine.dispose()
