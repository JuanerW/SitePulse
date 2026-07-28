# SitePulse

SitePulse is a full-stack website health monitoring platform. Upload a CSV of
URLs, process checks asynchronously, follow live progress, inspect actionable
failure categories, retry selected results, and export the final report.

Current version: **0.4.1 — Correlated JSON Logging**

## Architecture

| Component | Technology | Responsibility |
|---|---|---|
| Web UI | React, TypeScript, Vite | Uploads, progress, history, analytics, and result actions |
| API | FastAPI | Validation, job creation, queries, and lifecycle commands |
| Queue | Redis | Delivers job messages from the API to workers |
| Worker | Celery | Checks URLs concurrently and persists progress |
| Database | PostgreSQL | Stores jobs and individual URL results |
| Local runtime | Docker Compose | Runs PostgreSQL and Redis |

```text
React ──POST──> FastAPI ──write──> PostgreSQL
                    │
                  job_id
                    ▼
                  Redis
                    │
                    ▼
              Celery Worker ──update──> PostgreSQL

React ──poll GET──> FastAPI ──read────> PostgreSQL
```

One CSV creates one job with multiple result rows:

```text
Job
├── CheckResult: URL 1
├── CheckResult: URL 2
└── CheckResult: URL N
```

## Product behavior

1. React uploads a CSV to `POST /api/checks`.
2. FastAPI validates the file and creates a queued job in PostgreSQL.
3. FastAPI sends the `job_id` to Redis and immediately returns `202 Accepted`.
4. A Celery worker reads queued URL rows and checks up to 10 concurrently.
5. Each completed result is committed so the dashboard can show live progress.
6. React polls `GET /api/jobs/{job_id}` once per second until the job is terminal.
7. Users can filter and sort results, view summary metrics, export CSV, retry all
   failures or selected failures, cancel active jobs, and delete terminal jobs.

Job lifecycle:

```text
queued → processing → completed
                   ↘ failed
         cancelled
```

An HTTP error or timeout is a result-level failure and does not necessarily
make the entire job fail.

Result categories:

```text
healthy
blocked
rate_limited
not_found
client_error
server_error
timeout
network_error
queued
```

## API

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/health` | Check the API process |
| `GET` | `/health/database` | Check PostgreSQL connectivity |
| `POST` | `/api/checks` | Create an asynchronous check job |
| `GET` | `/api/jobs` | Search and paginate job history |
| `GET` | `/api/jobs/{job_id}` | Read progress and results |
| `POST` | `/api/jobs/{job_id}/retry-failed` | Retry every non-healthy result |
| `POST` | `/api/jobs/{job_id}/retry-selected` | Retry selected result IDs |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel an active job |
| `DELETE` | `/api/jobs/{job_id}` | Delete a terminal job and its results |
| `GET` | `/api/jobs/{job_id}/export` | Export results as CSV |

Interactive API documentation is available at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Observability

Every HTTP response includes an `X-Request-ID`. Clients may supply this header;
otherwise the API generates a UUID. When an API request creates or retries a
job, its request ID is propagated through the Celery message.

FastAPI and Celery emit one JSON object per log line:

```json
{
  "timestamp": "2026-07-28T12:00:00+00:00",
  "level": "info",
  "logger": "sitepulse.worker",
  "event": "url_check_completed",
  "request_id": "f62f...",
  "job_id": "9104...",
  "result_id": 42,
  "result_status": "healthy",
  "duration_ms": 184
}
```

This makes one upload traceable across the API, Redis queue, and Celery worker.
Logs intentionally contain IDs, counts, statuses, and timings rather than CSV
contents or response bodies.

## Local development

Requirements: Python 3.11+, Node.js 20+, Docker Desktop.

Install backend dependencies:

```powershell
cd backend
python -m pip install -r requirements.txt
```

Start PostgreSQL and Redis from the repository root:

```powershell
docker compose up -d
docker compose ps
```

Apply database migrations:

```powershell
cd backend
python -m alembic upgrade head
```

The baseline migration creates a fresh schema and can safely adopt databases
created by SitePulse versions before `0.4.0`. Use `python -m alembic current`
to inspect the installed revision.

Start FastAPI:

```powershell
cd backend
python -m uvicorn main:app --reload
```

Start a Celery worker in a second terminal:

```powershell
cd backend
celery -A celery_app worker --loglevel=info --pool=solo
```

The `solo` pool is intended for Windows development. Production deployments
normally use Linux workers.

Start React in a third terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173).

## Tests

Backend tests use HTTPX `MockTransport` and never depend on live websites:

```powershell
cd backend
python -m pytest -q
```

Build and type-check the frontend:

```powershell
cd frontend
npm run build
```

The current backend baseline is **23 passing tests**, covering CSV parsing,
deduplication, URL validation, basic SSRF protection, response classification,
timeouts, network errors, and HTML title extraction.

Sample inputs:

- `backend/sample-websites.csv`: three URLs for a quick smoke test.
- `backend/sample-websites-150.csv`: 150 real websites for queue and progress testing.

Real websites may return `403`, `429`, or time out because they restrict
automated traffic even when they remain accessible in a browser.

## Code tour

1. `backend/main.py` — REST API and job lifecycle endpoints.
2. `backend/celery_app.py` — Redis and Celery configuration.
3. `backend/tasks.py` — background processing and progress persistence.
4. `backend/checker.py` — isolated URL validation and HTTP checking.
5. `backend/models.py` — SQLAlchemy persistence models.
6. `frontend/src/App.tsx` — dashboard state, polling, filtering, and actions.

## Roadmap

- API and worker integration tests
- Stronger idempotency and worker crash recovery
- Complete DNS/IP and redirect-aware SSRF protection
- Upload and response body size limits
- API rate limiting
- GitHub Actions CI
- One-command Docker Compose startup for the complete stack
- Public deployment and performance benchmarks

## Version history

### 0.4.1 — 2026-07-28

Added:

- Shared `observability.py` module with JSON formatting and correlation context.
- `X-Request-ID` generation, propagation, and response headers.
- Request ID propagation from FastAPI into Celery task messages.
- Structured API request, job lifecycle, retry, and URL completion events.
- Three observability tests; the backend baseline is now 23 tests.

Changed:

- FastAPI and Celery now use the same machine-readable logging format.
- Celery tasks accept an optional request ID while remaining compatible with
  previously queued one-argument messages.

### 0.4.0 — 2026-07-28

- Replaced startup-time `create_all` and handwritten SQL with Alembic.
- Added an asynchronous migration environment using `DATABASE_URL`.
- Added a compatibility-aware baseline for fresh and existing databases.
- Verified both adoption and clean-database upgrade paths.

### 0.3.1 — 2026-07-28

- Added individual result selection and select-all for the active filter.
- Added a selected-result retry endpoint with job ownership validation.
- Disabled selection for healthy and active results.
- Clear selections automatically when switching jobs.

### 0.3.0 — 2026-07-27

- Added actionable result categories, summary metrics, filtering, and sorting.
- Added searchable, paginated job history.
- Added retry-all, cancellation, deletion, and UTF-8 CSV export.
- Added cancellation checkpoints and migrated existing result categories.
- Expanded the backend test baseline to 20 tests.

### 0.2.0 — 2026-07-27

- Introduced Redis and Celery asynchronous processing.
- Added live progress polling and concurrent checks.

### 0.1.0 — 2026-07-26

- Built the React and FastAPI MVP with PostgreSQL persistence.

## Current limitations

- The dashboard polls instead of using SSE or WebSockets.
- Progress is committed once per URL; larger workloads should batch writes.
- SSRF protection is currently basic and does not fully pin DNS resolutions.
- Future schema changes must be added as Alembic revisions.
- FastAPI, Celery, and React are not yet included in Docker Compose.
