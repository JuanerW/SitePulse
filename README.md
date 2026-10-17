# SitePulse

[![CI](https://github.com/JuanerW/SitePulse/actions/workflows/ci.yml/badge.svg)](https://github.com/JuanerW/SitePulse/actions/workflows/ci.yml)

SitePulse is a full-stack website health monitoring platform. Paste a list of
URLs or upload a CSV, process checks asynchronously, follow live progress,
inspect actionable failure categories, retry selected results, and export the
final report.

Live demo: **https://sitepulse.hanyuuu.com**. Press "Run the 12-URL sample" to
see every result category, including a timeout and two blocked SSRF attempts.

Current version: **0.6.2 — A demo you can poke at**

## Architecture

| Component | Technology | Responsibility |
|---|---|---|
| Web UI | React, TypeScript, Vite | Uploads, progress, history, analytics, and result actions |
| API | FastAPI | Validation, job creation, queries, and lifecycle commands |
| Queue | Redis | Delivers job messages from the API to workers |
| Worker | Celery | Checks URLs concurrently and persists progress |
| Database | PostgreSQL | Stores jobs and individual URL results |
| Local runtime | Docker Compose | Runs migrations and the complete six-service stack |

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

1. React uploads a CSV to `POST /api/checks` (pasted URLs are turned into a
   CSV in the browser first).
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
unsafe_target        # private/reserved address, directly, via DNS, or via a redirect
response_too_large
queued
```

## API

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/health` | Check the API process |
| `GET` | `/health/database` | Check PostgreSQL connectivity |
| `GET` | `/api/system` | Workers online, queue depth, active jobs, version |
| `POST` | `/api/checks` | Create an asynchronous check job |
| `GET` | `/api/jobs` | Search and paginate the caller's own jobs |
| `GET` | `/api/jobs/{job_id}` | Read progress and results |
| `POST` | `/api/jobs/{job_id}/retry-failed` | Retry every non-healthy result |
| `POST` | `/api/jobs/{job_id}/retry-selected` | Retry selected result IDs |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel an active job |
| `DELETE` | `/api/jobs/{job_id}` | Delete a terminal job and its results |
| `GET` | `/api/jobs/{job_id}/export` | Export results as CSV |

Every request may carry `X-Owner-Token`, a random value the browser keeps in
localStorage. There are no accounts: the job list only returns jobs created
with the caller's token, and retry, cancel and delete return `403` for anyone
else. `GET /api/jobs/{id}` stays open, so a job link can be shared read-only.
Only a SHA-256 of the token is stored. Finished jobs are deleted after
`JOB_RETENTION_HOURS` (72 by default).

`/api/demo/status/{403|404|429|500|503}`, `/api/demo/slow`,
`/api/demo/redirect` and `/api/demo/redirect-to-private` are fixed targets
for the sample job, so it produces the same categories every time.

### Threat model for a public demo without accounts

- **Job links are capabilities.** Anyone holding a job URL can read its
  results and export them; job IDs are random UUIDv4s (122 bits), so they
  cannot be guessed or enumerated, and the list endpoint never shows other
  people's jobs. That is the same model as an unlisted document link.
- **The owner token is a bearer secret in localStorage.** Any script running
  on the page could read it, which is why the page loads no third-party code.
  Losing it (clearing site data) loses control of your jobs, not the data,
  which expires anyway. Only its SHA-256 is stored server-side.
- **No CSRF exposure.** Mutations require a custom `X-Owner-Token` header,
  which a cross-site form or image cannot send, and CORS allows only the
  local dev origins.
- **Real auth would change:** owner = user ID from a session, per-user
  quotas instead of per-IP, and an explicit "share" flag instead of open
  reads by ID.
- **The demo targets are deliberately harmless.** `/api/demo/slow` only sleeps
  (no slot limit to exhaust), and the private-network redirect points at
  `10.255.255.1`, not at a cloud metadata address, so the domain is not a
  ready-made SSRF helper.

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

## Reliability

### Idempotent uploads

Clients may send an `Idempotency-Key` header with `POST /api/checks`. Replaying
the same key and CSV returns the original job without sending duplicate Celery
work. Reusing the key with different content returns `409 Conflict`. A unique
PostgreSQL index resolves concurrent submissions atomically.

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/checks `
  -H "Idempotency-Key: upload-2026-07-28-001" `
  -F "file=@backend/sample-websites.csv"
```

### Worker recovery

- Celery acknowledges messages only after execution.
- Lost-worker messages are rejected and become eligible for redelivery.
- The worker writes `heartbeat_at` every 5 s while a job runs. A reaper (Celery
  beat, every 15 s) requeues any processing job whose heartbeat is older than
  30 s, keeping the original request ID. It also requeues jobs left
  `queued` for 60 s, which covers a Redis restart that lost messages:
  PostgreSQL is the source of truth, Redis only carries job IDs.
- PostgreSQL advisory locks allow only one worker to process a job at a time,
  so a requeue or redelivery while the first worker is alive is skipped.
- As a backstop, Redis also returns an unacknowledged message to the queue
  after the visibility timeout (360 s, longer than the longest legitimate
  job: 200 URLs, 10 at a time, each capped at 15 s).
- Redelivered jobs process only rows still marked `queued`.
- Each URL has a 15 s total deadline on top of httpx's per-read timeout, so a
  server that drips one byte at a time cannot hold a slot.
- The API reports the heartbeat's age (server clock), and the page shows a
  notice after 15 s of silence.
- Transient database and operating-system failures retry with exponential
  backoff; deterministic application failures fail immediately. Individual
  URLs are not retried automatically: a timeout or a 429 is a result, and the
  user decides whether to retry it.

Together these rules provide at-least-once delivery without duplicate progress
counts and allow a replacement worker to resume after a crash.

Measured on the local Compose stack with production settings, using a 40-URL
job of `httpbin.org/delay/3` links:

```text
05:12:03  job_processing_started   url_count=40
05:12:10  docker kill -s KILL sitepulse-worker-1      (20 / 40 saved)
05:12:10  docker start sitepulse-worker-1
05:12:41  stale_job_requeued       (heartbeat 30 s old)
05:12:41  job_processing_started   url_count=20       (only the queued rows)
05:12:48  job_completed            successful_urls=40 (no double counting)
```

Before the reaper, the same test waited for Redis redelivery: 93 s with a
40 s visibility timeout, and up to an hour with the original setting.

To repeat it: start a job, run `docker kill -s KILL <worker>` and then
`docker start <worker>`, and grep the worker log for the job's request ID.

The demo runs one worker container with the beat scheduler inside it
(`worker -B`), so recovery happens when that container comes back. With two or
more workers and a separate beat process, a surviving worker would pick the
job up without any restart.

## Security and resource controls

- CSV upload size: 1 MB
- URLs per job: 200
- URL length: 2,048 characters
- Downloaded response body: 1 MB
- Redirects: 5
- Create-job rate: 10 requests per client IP per minute
- URL schemes: HTTP and HTTPS only
- URL credentials are rejected
- Literal and DNS-resolved private, loopback, link-local, multicast, and
  reserved IPv4/IPv6 destinations are rejected
- The worker connects to the exact IP it validated (the Host header and TLS
  SNI keep the original name), so a DNS answer that changes between the check
  and the connection (DNS rebinding) cannot reach a private address
- Every redirect target is validated and resolved again before following it
- Create and retry are rate-limited separately; a job can be retried 5 times

The rate limiter is stored in Redis and returns `429` with `Retry-After`.
Limits are configurable through environment variables. It keys on the real
visitor address: the frontend nginx trusts `X-Real-IP` only from private
addresses (the host's edge proxy), and the API trusts it only from the nginx
container. An idempotent replay is answered before the limiter and costs
nothing.

## Local development

Requirements: Python 3.11+, Node.js 20+, Docker Desktop.

Install backend dependencies:

```powershell
cd backend
python -m pip install -r requirements.txt
```

### One-command Docker startup

Build and start the complete application:

```powershell
docker compose up --build
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Compose starts:

```text
postgres → migrate → api
redis ─────────────→ api + worker
api ───────────────→ frontend
```

The `migrate` container must complete successfully before the API and worker
start. PostgreSQL and Redis use persistent named volumes.

### Manual development

Start PostgreSQL and Redis:

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

The current backend baseline is **46 passing tests**, covering CSV parsing,
deduplication, API and PostgreSQL integration, idempotency, request IDs,
rate limiting and client-IP trust, per-browser job ownership, URL and DNS/IP
SSRF defenses including DNS rebinding and redirects into private ranges,
worker lock contention, the stale-job reaper, retention cleanup,
resume-after-crash, a slow-drip deadline, IPv6 /64 rate-limit buckets, size
limits, response
classification, network failures, and HTML title extraction.

GitHub Actions runs:

- PostgreSQL and Redis service containers
- Alembic upgrade and model drift detection
- Backend tests
- Frontend lint, type-check, and production build
- Docker Compose validation and image builds

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

- SSE or WebSocket progress updates
- Performance benchmarks

## Version history

### 0.6.2 — 2026-10-17

- Production uses the host's shared 1Panel Redis (database 1) instead of a
  Redis container inside this stack; local Compose still bundles its own.
- The reaper also requeues jobs stuck in `queued` for 60 s, so losing Redis
  data loses no work.

### 0.6.1 — 2026-10-17

- Heartbeat every 5 s from a timer, plus a Celery beat reaper that requeues
  jobs whose heartbeat is 30 s old. Kill-to-resume went from 93 s to 31 s.
- Visibility timeout raised to 360 s so it exceeds the longest job; duplicate
  deliveries are skipped by the advisory lock.
- 15 s total deadline per URL.
- Jobs store their request ID, so requeued work logs under the same ID.
- Retention runs on a 10-minute schedule; owner-less jobs go after 1 hour.
- Blocked and timed-out rows keep the redirect hop, refused target, last IP
  and elapsed time.
- Connections fall back to the next validated address (IPv4 first) when one
  refuses.
- IPv6 visitors are rate-limited per /64.
- `/api/system` counts in-flight messages and is cached for 5 s.
- The demo slow endpoint has no slot cap to exhaust; the private redirect no
  longer targets the metadata address.
- Heartbeat age is computed by the server, not the visitor's clock.
- A requeue counts as a heartbeat, so a waiting job is not requeued every
  15 s; a failed heartbeat write is logged instead of stopping the loop.
- Timeout rows keep the address the worker was connected to.

### 0.6.0 — 2026-10-17

Added:

- A paste box and a one-click 12-URL sample that hits every result category,
  plus a downloadable sample CSV.
- Fixed demo targets under `/api/demo/` for that sample.
- Per-browser job ownership via `X-Owner-Token`: history, retry, cancel and
  delete are private, and job links can be shared read-only.
- 72-hour retention for finished jobs.
- An `unsafe_target` category; SSRF rejections no longer look like job failures.
- Connections pinned to the validated IP (DNS-rebinding fix).
- Final URL, connected IP and redirect count per result, shown in an
  expandable row and in the CSV export.
- `heartbeat_at` on jobs and a "worker went quiet" notice on the page.
- `/api/system` and a live status strip (workers, queue depth, version).
- An "Under the hood" panel: request ID, Idempotency-Key with a replay
  button, and the remaining rate-limit quota.
- p50/p95 response times and clickable category counts.
- Ten tests, bringing the backend baseline to 41.

Changed:

- The rate limit keys on the real visitor IP instead of the proxy's.
- Retries are rate-limited and capped at 5 per job.
- Idempotent replays are answered before the rate limiter.
- The visibility timeout went from 3600 s to 120 s; a duplicate delivery that
  finds the job locked retries later instead of being dropped.
- SQL echo is off unless `SQL_ECHO=1`.

### 0.5.0 — 2026-07-28

Added:

- PostgreSQL-backed API integration tests covering the complete job lifecycle.
- `Idempotency-Key` support with payload fingerprints and a unique DB index.
- PostgreSQL advisory locks for single-worker job ownership and crash recovery.
- Classified Celery retries with exponential backoff.
- Redis redelivery and visibility-timeout configuration.
- CSV, URL, response body, and redirect limits.
- DNS/IP and redirect-aware SSRF validation for IPv4 and IPv6.
- Redis-backed create-job rate limiting with standard response headers.
- GitHub Actions jobs for backend, frontend, and Compose verification.
- Production Dockerfiles, Nginx routing, migration container, and complete
  six-service Docker Compose startup.
- Eight additional tests, bringing the backend baseline to 31.

Changed:

- React now supports `VITE_API_URL` and uses the Nginx `/api` proxy in Docker.
- Website responses are streamed and stopped at the configured byte limit.
- Worker redelivery resumes only queued result rows.
- README now documents every reliability, security, CI, and deployment control.

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
- Ownership is a browser token, not an account: clearing site data loses
  access to your jobs (they still expire on schedule).
- Progress is committed once per URL; larger workloads should batch writes.
- DNS targets are validated immediately before each request, but hardened
  production deployments should also enforce outbound network policy.
- Future schema changes must be added as Alembic revisions.
- Rate limiting is IP-based; visitors behind one NAT share a quota.
