import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import './App.css'
import {
  API_URL,
  SAMPLE_URLS,
  apiFetch,
  newIdempotencyKey,
  parsePastedUrls,
  urlsToCsv,
  urlsToCsvFile,
} from './api'

const POLL_INTERVAL_MS = 1000
const SYSTEM_POLL_MS = 10000
const PAGE_SIZE = 8
// The worker writes a heartbeat every 5 s; the reaper requeues after 30 s.
const STALE_HEARTBEAT_SECONDS = 15
const MAX_URLS = 200

type ResultStatus =
  | 'queued'
  | 'healthy'
  | 'blocked'
  | 'rate_limited'
  | 'not_found'
  | 'server_error'
  | 'client_error'
  | 'timeout'
  | 'network_error'
  | 'response_too_large'
  | 'unsafe_target'

type CheckResult = {
  id: number
  url: string
  status: ResultStatus
  status_code: number | null
  response_time_ms: number | null
  title: string | null
  error: string | null
  final_url: string | null
  remote_ip: string | null
  redirect_count: number
}

type JobAccepted = {
  job_id: string
  status: string
  total_urls: number
  replayed: boolean
}

type JobDetail = {
  job_id: string
  filename: string
  status: string
  total: number
  completed: number
  successful: number
  failed: number
  error: string | null
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
  heartbeat_at?: string | null
  heartbeat_age_seconds?: number | null
  retry_count?: number
  is_owner: boolean
  results: CheckResult[]
}

type SystemStatus = {
  version: string
  commit: string
  workers: number
  queue_depth: number
  in_flight: number
  active_jobs: number
  retention_hours: number
}

// What the page remembers about its own last upload, for the
// "Under the hood" panel.
type Submission = {
  jobId: string
  file: File
  idempotencyKey: string
  requestId: string | null
  rateLimit: string | null
  rateRemaining: string | null
  replay?: { jobId: string; replayed: boolean; requestId: string | null }
}

type InputMode = 'paste' | 'csv'

type JobSummary = {
  id: string
  filename: string
  status: string
  total_urls: number
  completed_urls: number
  successful_urls: number
  failed_urls: number
  created_at: string
}

type JobPage = {
  items: JobSummary[]
  total: number
  page: number
  page_size: number
  pages: number
}

type SortOption = 'response_desc' | 'response_asc' | 'url' | 'status'

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  processing: 'Checking',
  completed: 'Completed',
  cancelled: 'Cancelled',
  failed: 'Job failed',
  healthy: 'Healthy',
  blocked: 'Blocked',
  rate_limited: 'Rate limited',
  not_found: 'Not found',
  server_error: 'Server error',
  client_error: 'Client error',
  timeout: 'Timeout',
  network_error: 'Network error',
  response_too_large: 'Response too large',
  unsafe_target: 'Unsafe target',
}

function percentile(sortedValues: number[], fraction: number) {
  if (sortedValues.length === 0) return 0
  const index = Math.min(
    sortedValues.length - 1,
    Math.ceil(fraction * sortedValues.length) - 1,
  )
  return sortedValues[Math.max(0, index)]
}

async function readError(response: Response, fallback: string) {
  try {
    const body = await response.json()
    if (typeof body.detail === 'string') return body.detail
  } catch {
    // Non-JSON error page from a proxy.
  }
  return `${fallback} (HTTP ${response.status})`
}

function jobIdFromLocation() {
  return new URLSearchParams(window.location.search).get('job')
}

function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status
}

function App() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [data, setData] = useState<JobDetail | null>(null)
  const [jobPage, setJobPage] = useState<JobPage>({
    items: [],
    total: 0,
    page: 1,
    page_size: PAGE_SIZE,
    pages: 1,
  })
  const [error, setError] = useState('')
  const [historyError, setHistoryError] = useState('')
  const [notice, setNotice] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isHistoryLoading, setIsHistoryLoading] = useState(true)
  const [loadingJobId, setLoadingJobId] = useState<string | null>(null)
  const [actionName, setActionName] = useState('')
  const [historyStatus, setHistoryStatus] = useState('')
  const [historyQuery, setHistoryQuery] = useState('')
  const [page, setPage] = useState(1)
  const [resultStatus, setResultStatus] = useState('all')
  const [sortBy, setSortBy] = useState<SortOption>('response_desc')
  const [inputMode, setInputMode] = useState<InputMode>('paste')
  const [pastedText, setPastedText] = useState('')
  const [submission, setSubmission] = useState<Submission | null>(null)
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [systemDown, setSystemDown] = useState(false)
  const [expandedResultId, setExpandedResultId] = useState<number | null>(null)
  const [selectedResultIds, setSelectedResultIds] = useState<Set<number>>(
    new Set(),
  )

  const loadHistory = useCallback(
    async (
      requestedPage = page,
      requestedStatus = historyStatus,
      requestedQuery = historyQuery,
    ) => {
      setIsHistoryLoading(true)
      setHistoryError('')

      const params = new URLSearchParams({
        page: String(requestedPage),
        page_size: String(PAGE_SIZE),
      })
      if (requestedStatus) params.set('status', requestedStatus)
      if (requestedQuery.trim()) params.set('q', requestedQuery.trim())

      try {
        const response = await apiFetch(`/api/jobs?${params}`)
        const responseData = await response.json()
        if (!response.ok) {
          throw new Error(responseData.detail ?? 'Failed to load job history.')
        }
        setJobPage(responseData as JobPage)
      } catch (requestError) {
        setHistoryError(
          requestError instanceof Error
            ? requestError.message
            : 'Could not connect to the API.',
        )
      } finally {
        setIsHistoryLoading(false)
      }
    },
    [historyQuery, historyStatus, page],
  )

  const loadJob = useCallback(
    async (jobId: string, showLoading = true) => {
      if (showLoading) setLoadingJobId(jobId)
      setHistoryError('')

      try {
        const response = await apiFetch(`/api/jobs/${jobId}`)
        if (!response.ok) {
          throw new Error(await readError(response, 'Failed to load job details.'))
        }

        const detail = (await response.json()) as JobDetail
        setData(detail)
        setJobPage((current) => ({
          ...current,
          items: current.items.map((job) =>
            job.id === detail.job_id
              ? {
                  ...job,
                  status: detail.status,
                  completed_urls: detail.completed,
                  successful_urls: detail.successful,
                  failed_urls: detail.failed,
                }
              : job,
          ),
        }))
      } catch (requestError) {
        setHistoryError(
          requestError instanceof Error
            ? requestError.message
            : 'Could not connect to the API.',
        )
      } finally {
        if (showLoading) setLoadingJobId(null)
      }
    },
    [],
  )

  // Debounce search by 300 ms to avoid a request on every keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadHistory()
    }, 300)
    return () => window.clearTimeout(timer)
  }, [loadHistory])

  // Poll active jobs every second and stop at a terminal status.
  useEffect(() => {
    if (!data || TERMINAL_STATUSES.has(data.status)) return
    const timer = window.setTimeout(() => {
      void loadJob(data.job_id, false)
    }, POLL_INTERVAL_MS)
    return () => window.clearTimeout(timer)
  }, [data, loadJob])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 3500)
    return () => window.clearTimeout(timer)
  }, [notice])

  // A selection belongs to one job only. Clear it when the user opens another
  // history item so a retry can never accidentally cross job boundaries.
  useEffect(() => {
    setSelectedResultIds(new Set())
    setExpandedResultId(null)
  }, [data?.job_id])

  // Open a shared link (?job=<id>) on first load.
  useEffect(() => {
    const sharedJobId = jobIdFromLocation()
    if (sharedJobId) void loadJob(sharedJobId)
  }, [loadJob])

  // Keep the address bar pointing at the open job so it can be shared.
  useEffect(() => {
    const url = new URL(window.location.href)
    if (data?.job_id) url.searchParams.set('job', data.job_id)
    else url.searchParams.delete('job')
    window.history.replaceState(null, '', url)
  }, [data?.job_id])

  useEffect(() => {
    let cancelled = false
    async function loadSystem() {
      try {
        const response = await apiFetch('/api/system')
        if (!response.ok) throw new Error('system status unavailable')
        const status = (await response.json()) as SystemStatus
        if (!cancelled) {
          setSystem(status)
          setSystemDown(false)
        }
      } catch {
        if (!cancelled) setSystemDown(true)
      }
    }
    void loadSystem()
    const timer = window.setInterval(() => void loadSystem(), SYSTEM_POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    setSelectedFile(event.target.files?.[0] ?? null)
    setError('')
  }

  async function postCheck(file: File, idempotencyKey: string) {
    const formData = new FormData()
    formData.append('file', file)
    return apiFetch('/api/checks', {
      method: 'POST',
      body: formData,
      headers: { 'Idempotency-Key': idempotencyKey },
    })
  }

  async function submitFile(file: File) {
    setIsSubmitting(true)
    setError('')
    const idempotencyKey = newIdempotencyKey()

    try {
      const response = await postCheck(file, idempotencyKey)
      if (!response.ok) {
        const message = await readError(response, 'Failed to create the check job.')
        throw new Error(
          response.status === 429
            ? `${message}. The limit is per visitor IP, ${response.headers.get('X-RateLimit-Limit') ?? 10} jobs a minute.`
            : message,
        )
      }

      const accepted = (await response.json()) as JobAccepted
      setSubmission({
        jobId: accepted.job_id,
        file,
        idempotencyKey,
        requestId: response.headers.get('X-Request-ID'),
        rateLimit: response.headers.get('X-RateLimit-Limit'),
        rateRemaining: response.headers.get('X-RateLimit-Remaining'),
      })
      setData({
        job_id: accepted.job_id,
        filename: file.name,
        status: accepted.status,
        total: accepted.total_urls,
        completed: 0,
        successful: 0,
        failed: 0,
        error: null,
        is_owner: true,
        results: [],
      })
      setPage(1)
      setNotice('Check job added to the queue')
      await loadHistory(1)
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Could not connect to the API.',
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (inputMode === 'csv') {
      if (!selectedFile) {
        setError('Choose a CSV file first.')
        return
      }
      await submitFile(selectedFile)
      return
    }

    const urls = parsePastedUrls(pastedText)
    if (urls.length === 0) {
      setError('Paste at least one URL, one per line.')
      return
    }
    if (urls.length > MAX_URLS) {
      setError(`That is ${urls.length} URLs; a job can check at most ${MAX_URLS}.`)
      return
    }
    await submitFile(urlsToCsvFile(urls, 'pasted-urls.csv'))
  }

  async function runSample() {
    await submitFile(
      urlsToCsvFile(
        SAMPLE_URLS.map((sample) => sample.url),
        'sitepulse-sample.csv',
      ),
    )
  }

  // Send the last upload again with the same Idempotency-Key. The API should
  // answer with the same job and queue nothing new.
  async function replayLastSubmission() {
    if (!submission) return
    setActionName('replay')
    setError('')
    try {
      const response = await postCheck(submission.file, submission.idempotencyKey)
      if (!response.ok) {
        throw new Error(await readError(response, 'Replay failed.'))
      }
      const accepted = (await response.json()) as JobAccepted
      setSubmission({
        ...submission,
        replay: {
          jobId: accepted.job_id,
          replayed: accepted.replayed,
          requestId: response.headers.get('X-Request-ID'),
        },
      })
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Replay failed.')
    } finally {
      setActionName('')
    }
  }

  async function copyShareLink() {
    try {
      await navigator.clipboard.writeText(window.location.href)
      setNotice('Link copied. Anyone with it can view this job, read-only.')
    } catch {
      setNotice('Copy the address bar to share this job.')
    }
  }

  async function runJobAction(
    action: 'retry-failed' | 'cancel',
    successMessage: string,
  ) {
    if (!data) return
    setActionName(action)
    try {
      const response = await apiFetch(`/api/jobs/${data.job_id}/${action}`, {
        method: 'POST',
      })
      if (!response.ok) {
        throw new Error(await readError(response, 'Action failed.'))
      }
      const responseData = await response.json()
      setNotice(responseData.message ?? successMessage)
      await loadJob(data.job_id, false)
      await loadHistory()
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Action failed.',
      )
    } finally {
      setActionName('')
    }
  }

  async function retrySelectedResults() {
    if (!data || selectedResultIds.size === 0) return
    setActionName('retry-selected')
    setError('')

    try {
      const response = await apiFetch(`/api/jobs/${data.job_id}/retry-selected`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          result_ids: Array.from(selectedResultIds),
        }),
      })
      if (!response.ok) {
        throw new Error(await readError(response, 'Failed to retry selected results.'))
      }
      const responseData = await response.json()

      setSelectedResultIds(new Set())
      setNotice(responseData.message ?? 'Selected results resubmitted')
      await loadJob(data.job_id, false)
      await loadHistory()
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Action failed.',
      )
    } finally {
      setActionName('')
    }
  }

  async function deleteSelectedJob() {
    if (!data) return
    const confirmed = window.confirm(
      `Delete "${data.filename}" and all its results? This action cannot be undone.`,
    )
    if (!confirmed) return

    setActionName('delete')
    try {
      const response = await apiFetch(`/api/jobs/${data.job_id}`, {
        method: 'DELETE',
      })
      if (!response.ok) {
        throw new Error(await readError(response, 'Delete failed.'))
      }
      setData(null)
      setSubmission(null)
      setNotice('Job deleted')
      await loadHistory()
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : 'Delete failed.',
      )
    } finally {
      setActionName('')
    }
  }

  const progress =
    data && data.total > 0
      ? Math.round((data.completed / data.total) * 100)
      : 0

  const summary = useMemo(() => {
    if (!data) return null
    const completedResults = data.results.filter(
      (result) => result.response_time_ms !== null,
    )
    const times = completedResults
      .map((result) => result.response_time_ms ?? 0)
      .sort((a, b) => a - b)
    const counts = new Map<string, number>()
    data.results.forEach((result) => {
      if (result.status !== 'queued') {
        counts.set(result.status, (counts.get(result.status) ?? 0) + 1)
      }
    })
    const slowest = completedResults.reduce<CheckResult | null>(
      (current, result) =>
        !current ||
        (result.response_time_ms ?? 0) > (current.response_time_ms ?? 0)
          ? result
          : current,
      null,
    )
    const blocked = data.results.filter((result) =>
      ['blocked', 'rate_limited'].includes(result.status),
    ).length
    return {
      healthyRate:
        data.completed > 0
          ? Math.round((data.successful / data.completed) * 100)
          : 0,
      p50: percentile(times, 0.5),
      p95: percentile(times, 0.95),
      slowest,
      blocked,
      categories: Array.from(counts.entries()).sort((a, b) => b[1] - a[1]),
    }
  }, [data])

  const visibleResults = useMemo(() => {
    if (!data) return []
    const filtered =
      resultStatus === 'all'
        ? [...data.results]
        : data.results.filter((result) => result.status === resultStatus)

    return filtered.sort((a, b) => {
      if (sortBy === 'url') return a.url.localeCompare(b.url)
      if (sortBy === 'status') return a.status.localeCompare(b.status)
      const aTime = a.response_time_ms ?? -1
      const bTime = b.response_time_ms ?? -1
      return sortBy === 'response_asc' ? aTime - bTime : bTime - aTime
    })
  }, [data, resultStatus, sortBy])

  const selectableVisibleResults = useMemo(
    () =>
      TERMINAL_STATUSES.has(data?.status ?? '')
        ? visibleResults.filter(
            (result) =>
              result.status !== 'healthy' && result.status !== 'queued',
          )
        : [],
    [data?.status, visibleResults],
  )
  const allVisibleSelected =
    selectableVisibleResults.length > 0 &&
    selectableVisibleResults.every((result) =>
      selectedResultIds.has(result.id),
    )

  function toggleResult(resultId: number) {
    setSelectedResultIds((current) => {
      const next = new Set(current)
      if (next.has(resultId)) next.delete(resultId)
      else next.add(resultId)
      return next
    })
  }

  function toggleAllVisibleResults() {
    setSelectedResultIds((current) => {
      const next = new Set(current)
      if (allVisibleSelected) {
        selectableVisibleResults.forEach((result) => next.delete(result.id))
      } else {
        selectableVisibleResults.forEach((result) => next.add(result.id))
      }
      return next
    })
  }

  const isOwner = Boolean(data?.is_owner)
  const canRetry =
    data &&
    isOwner &&
    TERMINAL_STATUSES.has(data.status) &&
    data.results.some((result) => result.status !== 'healthy')
  const canCancel = data && isOwner && ['queued', 'processing'].includes(data.status)
  const canDelete = data && isOwner && TERMINAL_STATUSES.has(data.status)
  const heartbeatAge =
    data?.status === 'processing' ? (data.heartbeat_age_seconds ?? 0) : 0
  const workerQuiet = heartbeatAge > STALE_HEARTBEAT_SECONDS
  const showSubmission = submission && data?.job_id === submission.jobId
  const sampleCsvHref = useMemo(
    () =>
      `data:text/csv;charset=utf-8,${encodeURIComponent(
        urlsToCsv(SAMPLE_URLS.map((sample) => sample.url)),
      )}`,
    [],
  )

  return (
    <main className="app-shell">
      <header className="site-header">
        <a className="brand" href="/">
          <span className="brand-mark">S</span>
          SitePulse
        </a>
        <span
          className="status"
          title={
            system
              ? `API ${system.version}, commit ${system.commit}. Jobs are kept ${system.retention_hours} hours.`
              : undefined
          }
        >
          <span
            className={`status-dot ${
              systemDown || (system && system.workers === 0) ? 'status-dot-down' : ''
            }`}
          />
          {systemDown
            ? 'API unreachable'
            : system
              ? `${system.workers} ${system.workers === 1 ? 'worker' : 'workers'} · ${system.active_jobs} active ${system.active_jobs === 1 ? 'job' : 'jobs'} · ${system.queue_depth >= 0 ? `${system.queue_depth} queued · ${system.in_flight} running` : 'queue unknown'}`
              : 'Connecting…'}
          {system && <code className="status-version">v{system.version} · {system.commit}</code>}
        </span>
      </header>

      {notice && <div className="toast">{notice}</div>}

      <section className="hero">
        <p className="eyebrow">WEBSITE HEALTH CHECKER</p>
        <h1>
          Check every URL.
          <br />
          Understand every result.
        </h1>
        <p className="hero-copy">
          Paste a list of URLs or upload a CSV. Celery workers check them in the
          background, and each result lands in a category you can act on.
        </p>
      </section>

      <section className="upload-card">
        <div className="input-tabs" role="tablist" aria-label="Input method">
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === 'paste'}
            className={inputMode === 'paste' ? 'input-tab input-tab-active' : 'input-tab'}
            onClick={() => setInputMode('paste')}
          >
            Paste URLs
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === 'csv'}
            className={inputMode === 'csv' ? 'input-tab input-tab-active' : 'input-tab'}
            onClick={() => setInputMode('csv')}
          >
            Upload CSV
          </button>
        </div>
        <form onSubmit={handleSubmit}>
          {inputMode === 'paste' ? (
            <textarea
              className="url-input"
              rows={5}
              spellCheck={false}
              placeholder={'example.com\nhttps://github.com\nhttps://your-site.dev/health'}
              value={pastedText}
              onChange={(event) => {
                setPastedText(event.target.value)
                setError('')
              }}
            />
          ) : (
            <label className="file-picker">
              <span className="upload-icon">↑</span>
              <span className="file-title">
                {selectedFile ? selectedFile.name : 'Choose CSV file'}
              </span>
              <span className="file-help">Needs a url column; up to {MAX_URLS} rows per job</span>
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={handleFileChange}
              />
            </label>
          )}
          <button className="check-button" type="submit" disabled={isSubmitting}>
            {isSubmitting ? 'Creating…' : 'Start checking'}
          </button>
        </form>
        <div className="sample-row">
          <button
            className="sample-button"
            type="button"
            disabled={isSubmitting}
            onClick={() => void runSample()}
          >
            No list handy? Run the {SAMPLE_URLS.length}-URL sample
          </button>
          <span className="sample-help">
            Covers every category, including a timeout and two SSRF attempts.{' '}
            <a href={sampleCsvHref} download="sitepulse-sample.csv">
              Download it as CSV
            </a>
          </span>
        </div>
        {error && <p className="error-message">{error}</p>}
      </section>

      {data && (
        <>
          <section className="progress-card" aria-live="polite">
            <div className="progress-heading">
              <div>
                <span className={`job-status job-status-${data.status}`}>
                  {statusLabel(data.status)}
                </span>
                <strong>{data.filename}</strong>
              </div>
              <strong>{progress}%</strong>
            </div>
            <div
              className="progress-track"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress}
            >
              <div className="progress-fill" style={{ width: `${progress}%` }} />
            </div>
            <div className="progress-meta">
              <span>
                Completed {data.completed} / {data.total}
              </span>
              <span className="success-text">{data.successful} Healthy</span>
              <span className="failure-text">{data.failed} need attention</span>
              {Boolean(data.retry_count) && <span>Retried {data.retry_count}×</span>}
            </div>
            {data.status === 'queued' && system && system.workers === 0 && (
              <p className="worker-note">
                No worker is online. The job stays queued in Redis and starts when one comes back.
              </p>
            )}
            {workerQuiet && (
              <p className="worker-note">
                No heartbeat for {Math.round(heartbeatAge)} s, so the worker
                probably died. Within about 30 s the reaper puts the job back on
                the queue, and the next worker skips the {data.completed} URLs
                already saved and checks the rest.
              </p>
            )}
            {!isOwner && (
              <p className="shared-note">
                Shared view, read-only. Only the browser that started this job can
                retry, cancel or delete it.
              </p>
            )}
            <div className="job-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => void copyShareLink()}
              >
                Copy link
              </button>
              <a
                className="secondary-button"
                href={`${API_URL}/api/jobs/${data.job_id}/export`}
                download
              >
                Download CSV
              </a>
              {canRetry && (
                <button
                  className="secondary-button"
                  type="button"
                  disabled={Boolean(actionName)}
                  onClick={() =>
                    void runJobAction('retry-failed', 'Failed results resubmitted')
                  }
                >
                  {actionName === 'retry-failed' ? 'Submitting…' : 'Retry all non-healthy'}
                </button>
              )}
              {canCancel && (
                <button
                  className="secondary-button"
                  type="button"
                  disabled={Boolean(actionName)}
                  onClick={() => void runJobAction('cancel', 'Job cancelled')}
                >
                  {actionName === 'cancel' ? 'Cancelling…' : 'Cancel job'}
                </button>
              )}
              {canDelete && (
                <button
                  className="danger-button"
                  type="button"
                  disabled={Boolean(actionName)}
                  onClick={() => void deleteSelectedJob()}
                >
                  {actionName === 'delete' ? 'Deleting…' : 'Delete'}
                </button>
              )}
            </div>
            {data.error && <p className="error-message">{data.error}</p>}
          </section>

          {showSubmission && submission && (
            <details className="hood-card">
              <summary>Under the hood: request ID, idempotency, rate limit</summary>
              <dl className="hood-list">
                <dt>Request ID</dt>
                <dd>
                  <code>{submission.requestId ?? 'not exposed'}</code>
                  <span>
                    The API logs it, and the Celery message carries it to the worker,
                    so every log line for this job, on either side, can be found
                    with one grep.
                  </span>
                </dd>
                <dt>Idempotency-Key</dt>
                <dd>
                  <code>{submission.idempotencyKey}</code>
                  <span>
                    A unique index on this key means a retried upload returns the
                    original job instead of queuing the same work twice.
                  </span>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={Boolean(actionName)}
                    onClick={() => void replayLastSubmission()}
                  >
                    {actionName === 'replay' ? 'Sending…' : 'Send the same upload again'}
                  </button>
                  {submission.replay && (
                    <span
                      className={
                        submission.replay.replayed && submission.replay.jobId === submission.jobId
                          ? 'success-text'
                          : 'failure-text'
                      }
                    >
                      {submission.replay.replayed && submission.replay.jobId === submission.jobId
                        ? `Same job ${submission.replay.jobId.slice(0, 8)}… came back and nothing new was queued. This request's ID was ${submission.replay.requestId?.slice(0, 8) ?? '?'}…; it did not count against the rate limit.`
                        : `Got job ${submission.replay.jobId.slice(0, 8)}…, which is not the original.`}
                    </span>
                  )}
                </dd>
                <dt>Rate limit</dt>
                <dd>
                  <code>
                    {submission.rateRemaining ?? '?'} of {submission.rateLimit ?? '?'} left
                  </code>
                  <span>
                    New jobs per visitor IP per minute, counted in Redis. Retries
                    have their own budget, and a job can be retried at most 5 times.
                  </span>
                </dd>
              </dl>
            </details>
          )}

          {summary && (
            <section className="summary-grid">
              <article className="metric-card metric-primary">
                <span>Health rate</span>
                <strong>{summary.healthyRate}%</strong>
                <small>Based on completed results</small>
              </article>
              <article className="metric-card">
                <span>Response time p50 / p95</span>
                <strong>
                  {summary.p50 ? `${summary.p50} / ${summary.p95} ms` : '—'}
                </strong>
                <small>Sites that answered, including redirects</small>
              </article>
              <article className="metric-card">
                <span>Blocked / rate limited</span>
                <strong>{summary.blocked}</strong>
                <small>Online but refusing checks</small>
              </article>
              <article className="metric-card">
                <span>Slowest response</span>
                <strong>
                  {summary.slowest?.response_time_ms
                    ? `${summary.slowest.response_time_ms} ms`
                    : '—'}
                </strong>
                <small title={summary.slowest?.url}>
                  {summary.slowest?.url ?? 'Waiting for results'}
                </small>
              </article>
              {summary.categories.length > 0 && (
                <div className="category-bar" aria-label="Results by category">
                  {summary.categories.map(([category, count]) => (
                    <button
                      key={category}
                      type="button"
                      className={`badge badge-${category} ${
                        resultStatus === category ? 'badge-active' : ''
                      }`}
                      onClick={() =>
                        setResultStatus((current) => (current === category ? 'all' : category))
                      }
                    >
                      {statusLabel(category)} {count}
                    </button>
                  ))}
                </div>
              )}
            </section>
          )}
        </>
      )}

      <section className="history-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">SAVED IN POSTGRESQL</p>
            <h2>Your jobs</h2>
            <p className="section-help">
              Only this browser sees them. Finished jobs are deleted after{' '}
              {system?.retention_hours ?? 72} hours.
            </p>
          </div>
          <span className="muted-count">{jobPage.total} jobs</span>
        </div>

        <div className="history-toolbar">
          <input
            type="search"
            placeholder="Search filenames"
            value={historyQuery}
            onChange={(event) => {
              setHistoryQuery(event.target.value)
              setPage(1)
            }}
          />
          <select
            value={historyStatus}
            onChange={(event) => {
              setHistoryStatus(event.target.value)
              setPage(1)
            }}
          >
            <option value="">All statuses</option>
            <option value="queued">Queued</option>
            <option value="processing">Checking</option>
            <option value="completed">Completed</option>
            <option value="cancelled">Cancelled</option>
            <option value="failed">Job failed</option>
          </select>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void loadHistory()}
            disabled={isHistoryLoading}
          >
            {isHistoryLoading ? 'Loading…' : 'Refresh'}
          </button>
        </div>

        {historyError && <p className="error-message">{historyError}</p>}

        {!isHistoryLoading && jobPage.items.length === 0 ? (
          <div className="empty-state">
            {historyQuery || historyStatus
              ? 'No jobs match these filters.'
              : 'No jobs from this browser yet. Run the sample above to see one.'}
          </div>
        ) : (
          <div className="history-list">
            {jobPage.items.map((job) => (
              <button
                className={`history-row ${
                  data?.job_id === job.id ? 'history-row-active' : ''
                }`}
                key={job.id}
                type="button"
                onClick={() => void loadJob(job.id)}
                disabled={loadingJobId === job.id}
              >
                <span className="history-main">
                  <strong>{job.filename}</strong>
                  <span>{new Date(job.created_at).toLocaleString()}</span>
                </span>
                <span className={`job-status job-status-${job.status}`}>
                  {statusLabel(job.status)}
                </span>
                <span className="history-count">
                  {job.completed_urls}/{job.total_urls}
                </span>
                <span className="success-text">
                  {job.successful_urls} Healthy
                </span>
                <span className="failure-text">
                  {job.failed_urls} attention
                </span>
                <span className="history-action">
                  {loadingJobId === job.id ? 'Loading…' : 'View →'}
                </span>
              </button>
            ))}
          </div>
        )}

        <div className="pagination">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => setPage((current) => current - 1)}
          >
            ← Previous
          </button>
          <span>
            Page {jobPage.page} / {jobPage.pages}
          </span>
          <button
            type="button"
            disabled={page >= jobPage.pages}
            onClick={() => setPage((current) => current + 1)}
          >
            Next →
          </button>
        </div>
      </section>

      {data && data.results.length > 0 && (
        <section className="results-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">DETAILED RESULTS</p>
              <h2>Check results</h2>
              <p className="job-id">Job {data.job_id}</p>
            </div>
            <span className="muted-count">{visibleResults.length} results</span>
          </div>

          <div className="results-toolbar">
            <select
              value={resultStatus}
              onChange={(event) => setResultStatus(event.target.value)}
            >
              <option value="all">All categories</option>
              <option value="healthy">Healthy</option>
              <option value="blocked">Blocked</option>
              <option value="rate_limited">Rate limited</option>
              <option value="not_found">Not found</option>
              <option value="client_error">Client error</option>
              <option value="server_error">Server error</option>
              <option value="timeout">Timeout</option>
              <option value="network_error">Network error</option>
              <option value="unsafe_target">Unsafe target</option>
              <option value="response_too_large">Response too large</option>
              <option value="queued">Queued</option>
            </select>
            <select
              value={sortBy}
              onChange={(event) => setSortBy(event.target.value as SortOption)}
            >
              <option value="response_desc">Response time: slowest first</option>
              <option value="response_asc">Response time: fastest first</option>
              <option value="url">URL: A–Z</option>
              <option value="status">Status</option>
            </select>
          </div>

          {selectableVisibleResults.length > 0 && (
            <div className="selection-bar" aria-live="polite">
              <span>
                Selected <strong>{selectedResultIds.size}</strong> non-healthy results
              </span>
              <div className="selection-actions">
                {selectedResultIds.size > 0 && (
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => setSelectedResultIds(new Set())}
                  >
                    Clear selection
                  </button>
                )}
                <button
                  className="secondary-button"
                  type="button"
                  disabled={
                    selectedResultIds.size === 0 || Boolean(actionName)
                  }
                  onClick={() => void retrySelectedResults()}
                >
                  {actionName === 'retry-selected'
                    ? 'Submitting…'
                    : `Retry selected (${selectedResultIds.size})`}
                </button>
              </div>
            </div>
          )}

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th className="select-cell">
                    <input
                      type="checkbox"
                      aria-label="Select all retryable results in the current filter"
                      checked={allVisibleSelected}
                      disabled={selectableVisibleResults.length === 0}
                      onChange={toggleAllVisibleResults}
                    />
                  </th>
                  <th>URL</th>
                  <th>Status</th>
                  <th>HTTP</th>
                  <th>Response time</th>
                  <th>Page title / error</th>
                </tr>
              </thead>
              <tbody>
                {visibleResults.map((result) => (
                  <Fragment key={result.id}>
                  <tr
                    className={
                      selectedResultIds.has(result.id) ? 'result-selected' : ''
                    }
                  >
                    <td className="select-cell">
                      <input
                        type="checkbox"
                        aria-label={`Select ${result.url}`}
                        checked={selectedResultIds.has(result.id)}
                        disabled={
                          !TERMINAL_STATUSES.has(data.status) ||
                          result.status === 'healthy' ||
                          result.status === 'queued'
                        }
                        onChange={() => toggleResult(result.id)}
                      />
                    </td>
                    <td className="url-cell">
                      <button
                        type="button"
                        className="url-toggle"
                        aria-expanded={expandedResultId === result.id}
                        title="Show connection details"
                        onClick={() =>
                          setExpandedResultId((current) =>
                            current === result.id ? null : result.id,
                          )
                        }
                      >
                        <span aria-hidden="true">
                          {expandedResultId === result.id ? '▾' : '▸'}
                        </span>
                        {result.url}
                      </button>
                    </td>
                    <td>
                      <span className={`badge badge-${result.status}`}>
                        {statusLabel(result.status)}
                      </span>
                    </td>
                    <td>{result.status_code ?? '—'}</td>
                    <td>
                      {result.response_time_ms === null
                        ? '—'
                        : `${result.response_time_ms} ms`}
                    </td>
                    <td className={result.error ? 'failure-text' : ''}>
                      <span className="title-cell" title={result.error ?? result.title ?? undefined}>
                        {result.status === 'queued'
                          ? 'Waiting for a worker'
                          : (result.error ?? result.title ?? '—')}
                      </span>
                    </td>
                  </tr>
                  {expandedResultId === result.id && (
                    <tr className="detail-row">
                      <td />
                      <td colSpan={5}>
                        <dl className="detail-list">
                          <dt>Requested</dt>
                          <dd>{result.url}</dd>
                          <dt>
                            {result.status === 'unsafe_target' && result.redirect_count > 0
                              ? 'Refused target'
                              : 'Final URL'}
                          </dt>
                          <dd>
                            {result.final_url ?? '—'}
                            {result.redirect_count > 0 &&
                              ` (after ${result.redirect_count} ${result.redirect_count === 1 ? 'redirect' : 'redirects'})`}
                          </dd>
                          <dt>
                            {result.status === 'unsafe_target' && result.redirect_count > 0
                              ? 'Last public hop'
                              : 'Connected to'}
                          </dt>
                          <dd>
                            {result.remote_ip ??
                              (result.status === 'unsafe_target'
                                ? 'Nothing. The address was refused before connecting.'
                                : '—')}
                          </dd>
                          {result.response_time_ms !== null && (
                            <>
                              <dt>Time</dt>
                              <dd>{result.response_time_ms} ms</dd>
                            </>
                          )}
                          <dt>Detail</dt>
                          <dd>{result.error ?? result.title ?? '—'}</dd>
                        </dl>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  )
}

export default App
