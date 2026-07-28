import csv
import io
import logging
import math
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
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


MAX_URLS = 200
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
    version="0.4.1",
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


class JobAccepted(BaseModel):
    """Immediate response after upload; processing may not have started yet."""

    job_id: uuid.UUID
    status: str
    total_urls: int


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
    results: list[CheckResult]


class ActionResponse(BaseModel):
    """A short confirmation for cancel, delete, and retry actions."""

    job_id: uuid.UUID
    status: str
    message: str


class RetrySelectedRequest(BaseModel):
    """The result row IDs that the user explicitly selected to retry."""

    result_ids: list[int] = Field(min_length=1, max_length=MAX_URLS)


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

    return {"status": "healthy"}


@app.get("/health/database")
async def database_health_check() -> dict[str, str]:
    """Run a minimal query to verify the PostgreSQL connection."""

    async with SessionFactory() as session:
        database_name = await session.scalar(text("SELECT current_database()"))

    return {"status": "healthy", "database": str(database_name)}


@app.post(
    "/api/checks",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_check_job(
    file: UploadFile = File(...),
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

    urls = read_urls_from_csv(await file.read())
    job = models.Job(
        filename=file.filename,
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
    await db.commit()
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
    db: AsyncSession = Depends(get_db),
) -> JobPage:
    """Filter job history by status and filename and return a page of results."""

    filters = []
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
        results=[
            CheckResult(
                id=result.id,
                url=result.url,
                status=result.status,
                status_code=result.status_code,
                response_time_ms=result.response_time_ms,
                title=result.title,
                error=result.error,
            )
            for result in job.results
        ],
    )


@app.post(
    "/api/jobs/{job_id}/retry-failed",
    response_model=ActionResponse,
)
async def retry_failed_results(
    job_id: uuid.UUID,
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
    if job.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="A running job cannot be retried")

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

    job.status = "queued"
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
    if job.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="A running job cannot be retried")

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

    retry_count = len(retry_records)
    job.status = "queued"
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
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Cancel a queued or processing job; workers stop at safe checkpoints."""

    job = await db.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")
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
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Delete a terminal job and all its results. Running jobs must be cancelled first."""

    job = await db.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Check job not found")
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
        ]
    )
    for result in job.results:
        writer.writerow(
            [
                result.url,
                result.status,
                result.status_code or "",
                result.response_time_ms or "",
                result.title or "",
                result.error or "",
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
