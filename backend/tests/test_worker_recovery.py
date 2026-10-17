"""Duplicate deliveries must wait for the running worker, never run twice or vanish."""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import models
import tasks


@pytest.mark.anyio
async def test_duplicate_delivery_raises_lock_busy_while_job_is_locked() -> None:
    engine = create_async_engine(tasks.DATABASE_URL)
    job_id = uuid.uuid4()
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_lock(hashtextextended(CAST(:job_id AS text), 0))"),
                {"job_id": str(job_id)},
            )
            # The "first worker" holds the lock; a redelivered copy must not
            # return quietly, or the job is lost if that worker dies later.
            with pytest.raises(tasks.JobLockBusy):
                await tasks.process_job(job_id)
    finally:
        await engine.dispose()


def test_lock_busy_is_retried_later_not_failed() -> None:
    def lock_busy(coroutine: object) -> None:
        coroutine.close()  # type: ignore[attr-defined]
        raise tasks.JobLockBusy()

    class Retried(Exception):
        pass

    with (
        patch.object(tasks.asyncio, "run", side_effect=lock_busy),
        patch.object(tasks.check_job, "retry", side_effect=Retried) as retry,
        patch.object(tasks, "mark_job_failed") as mark_failed,
    ):
        with pytest.raises(Retried):
            tasks.check_job.run(str(uuid.uuid4()))

    assert retry.call_args.kwargs["countdown"] == tasks.LOCK_BUSY_RETRY_SECONDS
    mark_failed.assert_not_called()


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
