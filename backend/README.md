# SitePulse Backend

The backend accepts CSV uploads, persists jobs in PostgreSQL, sends job IDs
through Redis, and uses Celery workers to check websites asynchronously.

## Run locally

From the repository root, start PostgreSQL and Redis:

```powershell
docker compose up -d
```

Start the API:

```powershell
cd backend
python -m pip install -r requirements.txt
python -m alembic upgrade head
python -m uvicorn main:app --reload
```

Start the Windows development worker in another terminal:

```powershell
cd backend
celery -A celery_app worker --loglevel=info --pool=solo
```

Open the API documentation at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Correlation and logs

The API returns `X-Request-ID` on every response. The same value is attached to
Celery work created by that request, while `job_id` identifies the persistent
background job. Both processes emit JSON logs to standard output.

Useful events include:

```text
request_completed
job_enqueued
celery_task_received
job_processing_started
url_check_completed
job_completed
```

## Processing flow

```text
CSV upload
    ↓
FastAPI creates a queued job
    ↓
Redis stores the task message
    ↓
Celery receives the job_id
    ↓
Worker checks URLs and updates PostgreSQL
    ↓
React polls GET /api/jobs/{job_id}
```

## Suggested reading order

1. `celery_app.py` — Celery and Redis configuration.
2. `main.py` — API validation, persistence, and task submission.
3. `tasks.py` — worker execution and progress updates.
4. `checker.py` — independent URL checking logic.
5. `models.py` — persistent job and result models.

## Tests

```powershell
python -m pytest -q
```

Tests use mocked HTTP transports and do not access the public internet.
