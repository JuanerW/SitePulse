import asyncio
import csv
import hashlib
import io
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from checker import validate_url
from database import SessionFactory, engine, get_db
from tasks import check_job
from observability import (
    RequestContextMiddleware,
    configure_logging,
    current_request_id,
)
from celery_app import celery_app
from rate_limit import (
    enforce_create_job_rate_limit,
    enforce_retry_rate_limit,
    redis_client,
)


MAX_URLS = 200
MAX_UPLOAD_BYTES = 1_000_000
MAX_URL_LENGTH = 2048
MAX_RETRIES_PER_JOB = 5
JOB_RETENTION_HOURS = int(os.getenv("JOB_RETENTION_HOURS", "72"))
APP_VERSION = "0.6.2"
GIT_SHA = os.getenv("GIT_SHA", "dev")
configure_logging()
logger = logging.getLogger("sitepulse.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """
    Release the shared database connection pool during graceful shutdown.

    Database schema changes are intentionally not applied here. Run
    `alembic upgrade head` before starting the API so every schema change is
    versioned, repeatable, and visible in deployment logs.
    """
    yield
    await engine.dispose()


app = FastAPI(
    title="SitePulse API",
    description="Upload a CSV of URLs and check website status in the background.",
    version=APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)

# Allow both local origins used by the React development server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After"],
)


class CheckResult(BaseModel):
    """The current or final check result for one URL."""

    id: int
    url: str
    status: str
    status_code: int | None = None
    response_time_ms: int | None = None
    title: str | None = None
    error: str | None = None
    final_url: str | None = None
    remote_ip: str | None = None
    redirect_count: int = 0


class JobAccepted(BaseModel):
    """Immediate response after upload; processing may not have started yet."""

    job_id: uuid.UUID
    status: str
    total_urls: int
    replayed: bool = False


class JobSummary(BaseModel):
    """One row in the job history."""

    id: uuid.UUID
    filename: str
    status: str
    total_urls: int
    completed_urls: int
    successful_urls: int
    failed_urls: int
    created_at: datetime


class JobPage(BaseModel):
    """A paginated job history response."""

    items: list[JobSummary]
    total: int
    page: int
    page_size: int
    pages: int


class JobDetail(BaseModel):
    """Job status, progress, and currently available results."""

    job_id: uuid.UUID
    filename: str
    status: str
    total: int
    completed: int
    successful: int
    failed: int
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    # Computed here so a visitor's wrong clock cannot fake a dead worker.
    heartbeat_age_seconds: float | None = None
    retry_count: int = 0
    # Whether the caller's owner token matches; only owners see job actions.
    is_owner: bool = False
    results: list[CheckResult]


class ActionResponse(BaseModel):
    """A short confirmation for cancel, delete, and retry actions."""

    job_id: uuid.UUID
    status: str
    message: str


class RetrySelectedRequest(BaseModel):
    """The result row IDs that the user explicitly selected to retry."""

    result_ids: list[int] = Field(min_length=1, max_length=MAX_URLS)


def owner_hash_from(token: str | None) -> str | None:
    """
    Hash the browser's random owner token.

    There are no accounts. Each browser keeps a random token in localStorage
    and sends it as X-Owner-Token; a job belongs to whoever created it. Job
    IDs are random UUIDs, so a job link can still be shared read-only.
    """

    if not token or not 16 <= len(token.strip()) <= 128:
        return None
    return hashlib.sha256(token.strip().encode()).hexdigest()


def require_owner(job: models.Job, owner_hash: str | None) -> None:
    if owner_hash is None or job.owner_hash != owner_hash:
        raise HTTPException(
            status_code=403,
            detail="Only the browser that started this job can change it",
        )


def read_urls_from_csv(content: bytes) -> list[str]:
    """Read, de-duplicate, and validate the URL column in a CSV."""

    try:
        csv_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="CSV must use UTF-8 encoding",
        ) from exc

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "url" not in reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV must contain a column named url")

    raw_urls = [
        row["url"].strip()
        for row in reader
        if row.get("url", "").strip()
    ]
    urls = list(dict.fromkeys(raw_urls))

    if not urls:
        raise HTTPException(status_code=400, detail="The CSV contains no URLs to check")
    if len(urls) > MAX_URLS:
        raise HTTPException(
            status_code=400,
            detail=f"A job can check at most {MAX_URLS} URLs",
        )
    if any(len(url) > MAX_URL_LENGTH for url in urls):
        raise HTTPException(
            status_code=400,
            detail=f"Each URL must be at most {MAX_URL_LENGTH} characters",
        )

    # Reject malformed or local URLs before creating a database job.
    for url in urls:
        try:
            validate_url(url)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{url}: {exc}",
            ) from exc

    return urls


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Confirm that the FastAPI process is responding."""

    return {"status": "healthy", "version": APP_VERSION, "commit": GIT_SHA}


@app.get("/health/database")
async def database_health_check() -> dict[str, str]:
    """Run a minimal query to verify the PostgreSQL connection."""

    async with SessionFactory() as session:
        database_name = await session.scalar(text("SELECT current_database()"))

    return {"status": "healthy", "database": str(database_name)}


_system_cache: dict[str, object] = {"at": 0.0, "body": None}
SYSTEM_CACHE_SECONDS = 5


async def count_workers() -> int:
    loop = asyncio.get_running_loop()
    try:
        replies = await loop.run_in_executor(
            None, lambda: celery_app.control.ping(timeout=0.5)
        )
        return len(replies or [])
    except Exception:
        logger.exception("worker_ping_failed")
        return 0


@app.get("/api/system")
async def system_status(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """
    Live numbers for the status strip, cached for 5 s across all visitors.

    `queued` counts messages waiting in Redis; `in_flight` counts messages a
    worker has taken but not acknowledged yet (acks are late, so a running
    job stays here until it finishes).
    """

    loop = asyncio.get_running_loop()
    if _system_cache["body"] and loop.time() - float(_system_cache["at"]) < SYSTEM_CACHE_SECONDS:
        return _system_cache["body"]  # type: ignore[return-value]
    try:
        queued = int(await redis_client.llen("celery"))
        in_flight = int(await redis_client.hlen("unacked"))
    except Exception:
        queued, in_flight = -1, -1
    active_jobs = int(
        await db.scalar(
            select(func.count(models.Job.id)).where(
                models.Job.status.in_(["queued", "processing"])
            )
        )
        or 0
    )
    body: dict[str, object] = {
        "version": APP_VERSION,
        "commit": GIT_SHA,
        "workers": await count_workers(),
        "queue_depth": queued,
        "in_flight": in_flight,
        "active_jobs": active_jobs,
        "retention_hours": JOB_RETENTION_HOURS,
    }
    _system_cache.update(at=loop.time(), body=body)
    return body


@app.post(
    "/api/checks",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_check_job(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=128,
    ),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> JobAccepted:
    """
    Create a background job and return immediately.

    This endpoint never contacts target websites. It only:
    1. parses the CSV;
    2. stores the job and queued URLs in PostgreSQL;
    3. sends the job_id to Redis.
    """

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"CSV files must be at most {MAX_UPLOAD_BYTES} bytes",
        )

    fingerprint = hashlib.sha256(content).hexdigest()
    owner_hash = owner_hash_from(owner_token)
    normalized_key = idempotency_key.strip() if idempotency_key else None
    if normalized_key:
        existing_job = await db.scalar(
            select(models.Job).where(
                models.Job.idempotency_key == normalized_key
            )
        )
        if existing_job:
            if (
                existing_job.request_fingerprint != fingerprint
                or existing_job.owner_hash != owner_hash
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used for a different request",
                )
            # A replay creates no work, so it is answered before the rate
            # limiter and does not spend the caller's quota.
            logger.info(
                "idempotent_job_reused",
                extra={"job_id": str(existing_job.id)},
            )
            return JobAccepted(
                job_id=existing_job.id,
                status=existing_job.status,
                total_urls=existing_job.total_urls,
                replayed=True,
            )

    await enforce_create_job_rate_limit(request, response)
    urls = read_urls_from_csv(content)
    job = models.Job(
        filename=file.filename,
        idempotency_key=normalized_key,
        request_fingerprint=fingerprint if normalized_key else None,
        owner_hash=owner_hash,
        request_id=current_request_id(),
        status="queued",
        total_urls=len(urls),
        completed_urls=0,
        successful_urls=0,
        failed_urls=0,
        results=[
            models.CheckResultRecord(url=url, status="queued")
            for url in urls
        ],
    )
    db.add(job)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if not normalized_key:
            raise
        # A concurrent request may have inserted the same key after our first
        # lookup. The unique index decides the winner atomically.
        existing_job = await db.scalar(
            select(models.Job).where(
                models.Job.idempotency_key == normalized_key
            )
        )
        if (
            existing_job is None
            or existing_job.request_fingerprint != fingerprint
            or existing_job.owner_hash != owner_hash
        ):
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used for a different request",
            )
        return JobAccepted(
            job_id=existing_job.id,
            status=existing_job.status,
            total_urls=existing_job.total_urls,
            replayed=True,
        )
    await db.refresh(job)

    try:
        # delay() sends a small message to Redis; it does not run the checks itself.
        check_job.delay(str(job.id), current_request_id())
        logger.info(
            "job_enqueued",
            extra={"job_id": str(job.id), "url_count": job.total_urls},
        )
    except Exception as exc:
        # The row already exists, so mark it failed instead of leaving it queued forever.
        job.status = "failed"
        job.error = "Could not send the job to Redis"
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="The task queue is temporarily unavailable",
        ) from exc

    return JobAccepted(
        job_id=job.id,
        status=job.status,
        total_urls=job.total_urls,
    )


@app.get("/api/jobs", response_model=JobPage)
async def list_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=8, ge=1, le=50),
    job_status: str | None = Query(default=None, alias="status"),
    query_text: str | None = Query(default=None, alias="q", max_length=100),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> JobPage:
    """Return a page of the caller's own jobs, filtered by status and filename."""

    owner_hash = owner_hash_from(owner_token)
    if owner_hash is None:
        return JobPage(items=[], total=0, page=page, page_size=page_size, pages=1)
    filters = [models.Job.owner_hash == owner_hash]
    if job_status:
        filters.append(models.Job.status == job_status)
    if query_text:
        filters.append(models.Job.filename.ilike(f"%{query_text.strip()}%"))

    count_query = select(func.count(models.Job.id)).where(*filters)
    total = int(await db.scalar(count_query) or 0)

    query = (
        select(models.Job)
        .where(*filters)
        .order_by(models.Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    jobs = (await db.scalars(query)).all()

    return JobPage(
        items=[
            JobSummary(
                id=job.id,
                filename=job.filename,
                status=job.status,
                total_urls=job.total_urls,
                completed_urls=job.completed_urls,
                successful_urls=job.successful_urls,
                failed_urls=job.failed_urls,
                created_at=job.created_at,
            )
            for job in jobs
        ],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, math.ceil(total / page_size)),
    )


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
async def get_job(
    job_id: uuid.UUID,
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> JobDetail:
    """Read job status, live progress, and completed results."""

    query = (
        select(models.Job)
        .options(selectinload(models.Job.results))
        .where(models.Job.id == job_id)
    )
    job = await db.scalar(query)

    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")

    return JobDetail(
        job_id=job.id,
        filename=job.filename,
        status=job.status,
        total=job.total_urls,
        completed=job.completed_urls,
        successful=job.successful_urls,
        failed=job.failed_urls,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        heartbeat_at=job.heartbeat_at,
        heartbeat_age_seconds=(
            round((datetime.now(timezone.utc) - job.heartbeat_at).total_seconds(), 1)
            if job.heartbeat_at
            else None
        ),
        retry_count=job.retry_count,
        is_owner=(
            job.owner_hash is not None
            and job.owner_hash == owner_hash_from(owner_token)
        ),
        results=[
            CheckResult(
                id=result.id,
                url=result.url,
                status=result.status,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
                title=result.title,
                error=result.error,
                final_url=result.final_url,
                remote_ip=result.remote_ip,
                redirect_count=result.redirect_count,
            )
            for result in sorted(job.results, key=lambda result: result.id)
        ],
    )


@app.post(
    "/api/jobs/{job_id}/retry-failed",
    response_model=ActionResponse,
)
async def retry_failed_results(
    job_id: uuid.UUID,
    _: None = Depends(enforce_retry_rate_limit),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Reset non-healthy URLs to queued and enqueue the same job again."""

    query = (
        select(models.Job)
        .options(selectinload(models.Job.results))
        .where(models.Job.id == job_id)
    )
    job = await db.scalar(query)

    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")
    require_owner(job, owner_hash_from(owner_token))
    if job.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="A running job cannot be retried")
    if job.retry_count >= MAX_RETRIES_PER_JOB:
        raise HTTPException(
            status_code=409,
            detail=f"A job can be retried at most {MAX_RETRIES_PER_JOB} times",
        )

    retry_records = [
        result
        for result in job.results
        if result.status != "healthy"
    ]
    if not retry_records:
        raise HTTPException(status_code=409, detail="There are no URLs to retry")

    previously_completed = sum(
        result.status != "queued"
        for result in retry_records
    )
    for result in retry_records:
        result.status = "queued"
        result.status_code = None
        result.response_time_ms = None
        result.title = None
        result.error = None
        result.final_url = None
        result.remote_ip = None
        result.redirect_count = 0

    job.status = "queued"
    job.retry_count += 1
    job.completed_urls -= previously_completed
    job.failed_urls = 0
    job.finished_at = None
    job.error = None
    await db.commit()

    try:
        check_job.delay(str(job.id), current_request_id())
        logger.info(
            "job_retry_all_enqueued",
            extra={"job_id": str(job.id), "url_count": len(retry_records)},
        )
    except Exception as exc:
        job.status = "failed"
        job.error = "Could not send the retry job to Redis"
        await db.commit()
        raise HTTPException(status_code=503, detail="The task queue is temporarily unavailable") from exc

    return ActionResponse(
        job_id=job.id,
        status=job.status,
        message=f"Resubmitted {len(retry_records)} URLs",
    )


@app.post(
    "/api/jobs/{job_id}/retry-selected",
    response_model=ActionResponse,
)
async def retry_selected_results(
    job_id: uuid.UUID,
    request: RetrySelectedRequest,
    _: None = Depends(enforce_retry_rate_limit),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Retry only the non-healthy result rows explicitly selected by the user."""

    query = (
        select(models.Job)
        .options(selectinload(models.Job.results))
        .where(models.Job.id == job_id)
    )
    job = await db.scalar(query)

    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")
    require_owner(job, owner_hash_from(owner_token))
    if job.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="A running job cannot be retried")
    if job.retry_count >= MAX_RETRIES_PER_JOB:
        raise HTTPException(
            status_code=409,
            detail=f"A job can be retried at most {MAX_RETRIES_PER_JOB} times",
        )

    # Every ID must belong to this job. Silently ignoring an invalid ID would
    # make the selection shown in the frontend disagree with the actual retry.
    requested_ids = list(dict.fromkeys(request.result_ids))
    records_by_id = {result.id: result for result in job.results}
    missing_ids = [
        result_id for result_id in requested_ids if result_id not in records_by_id
    ]
    if missing_ids:
        raise HTTPException(
            status_code=400,
            detail=f"{len(missing_ids)} result IDs do not belong to this job",
        )

    retry_records = [records_by_id[result_id] for result_id in requested_ids]
    invalid_records = [
        result
        for result in retry_records
        if result.status in {"healthy", "queued"}
    ]
    if invalid_records:
        raise HTTPException(
            status_code=409,
            detail="Only completed, non-healthy results can be retried",
        )

    for result in retry_records:
        result.status = "queued"
        result.status_code = None
        result.response_time_ms = None
        result.title = None
        result.error = None
        result.final_url = None
        result.remote_ip = None
        result.redirect_count = 0

    retry_count = len(retry_records)
    job.status = "queued"
    job.retry_count += 1
    job.completed_urls = max(0, job.completed_urls - retry_count)
    job.failed_urls = max(0, job.failed_urls - retry_count)
    job.finished_at = None
    job.error = None
    await db.commit()

    try:
        check_job.delay(str(job.id), current_request_id())
        logger.info(
            "job_retry_selected_enqueued",
            extra={"job_id": str(job.id), "url_count": retry_count},
        )
    except Exception as exc:
        job.status = "failed"
        job.error = "Could not send the selected retry job to Redis"
        await db.commit()
        raise HTTPException(status_code=503, detail="The task queue is temporarily unavailable") from exc

    return ActionResponse(
        job_id=job.id,
        status=job.status,
        message=f"Resubmitted {retry_count} selected URLs",
    )


@app.post("/api/jobs/{job_id}/cancel", response_model=ActionResponse)
async def cancel_job(
    job_id: uuid.UUID,
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Cancel a queued or processing job; workers stop at safe checkpoints."""

    job = await db.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")
    require_owner(job, owner_hash_from(owner_token))
    if job.status not in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="Only queued or processing jobs can be cancelled")

    job.status = "cancelled"
    job.finished_at = datetime.now().astimezone()
    await db.commit()

    return ActionResponse(
        job_id=job.id,
        status=job.status,
        message="Cancellation request saved",
    )


@app.delete("/api/jobs/{job_id}", response_model=ActionResponse)
async def delete_job(
    job_id: uuid.UUID,
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Delete a terminal job and all its results. Running jobs must be cancelled first."""

    job = await db.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")
    require_owner(job, owner_hash_from(owner_token))
    if job.status in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="Cancel the running job first")

    await db.delete(job)
    await db.commit()

    return ActionResponse(
        job_id=job_id,
        status="deleted",
        message="Job and results deleted",
    )


@app.get("/api/jobs/{job_id}/export")
async def export_job_csv(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Export the current job results as UTF-8 CSV."""

    query = (
        select(models.Job)
        .options(selectinload(models.Job.results))
        .where(models.Job.id == job_id)
    )
    job = await db.scalar(query)
    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "url",
            "status",
            "status_code",
            "response_time_ms",
            "title",
            "error",
            "final_url",
            "remote_ip",
            "redirect_count",
        ]
    )
    for result in sorted(job.results, key=lambda result: result.id):
        writer.writerow(
            [
                result.url,
                result.status,
                result.status_code or "",
                result.response_time_ms or "",
                result.title or "",
                result.error or "",
                result.final_url or "",
                result.remote_ip or "",
                result.redirect_count,
            ]
        )

    # A UTF-8 BOM helps Windows Excel detect the encoding reliably.
    content = "\ufeff" + output.getvalue()
    filename = f"sitepulse-{job.id}.csv"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Deterministic targets for the one-click sample job. The sample checks these
# through the public hostname, so each result category shows up every time
# without depending on third-party sites misbehaving on cue.
DEMO_STATUS_CODES = {200, 403, 404, 429, 500, 503}
DEMO_SLOW_SECONDS = 12  # longer than the checker's 8 s timeout


@app.get("/api/demo/status/{code}", include_in_schema=False)
async def demo_status(code: int = PathParam(...)) -> PlainTextResponse:
    if code not in DEMO_STATUS_CODES:
        raise HTTPException(status_code=404, detail="Unknown demo status")
    headers = {"Retry-After": "30"} if code == 429 else None
    return PlainTextResponse(f"SitePulse demo: HTTP {code}", status_code=code, headers=headers)


@app.get("/api/demo/slow", include_in_schema=False)
async def demo_slow() -> PlainTextResponse:
    # A sleeping coroutine costs almost nothing, so there is deliberately no
    # slot limit: a cap would let anyone fill it and turn every visitor's
    # "timeout" sample row into a 503.
    await asyncio.sleep(DEMO_SLOW_SECONDS)
    return PlainTextResponse("SitePulse demo: slow response")


@app.get("/api/demo/redirect", include_in_schema=False)
async def demo_redirect() -> RedirectResponse:
    return RedirectResponse("/api/demo/status/200", status_code=301)


@app.get("/api/demo/redirect-to-private", include_in_schema=False)
async def demo_redirect_to_private() -> RedirectResponse:
    # A public URL whose redirect points into a private network. The checker
    # must refuse the second hop. It deliberately does not point at the cloud
    # metadata address, so this domain is not a ready-made SSRF helper.
    return RedirectResponse("http://10.255.255.1/internal", status_code=302)
