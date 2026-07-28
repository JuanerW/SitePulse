import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import './App.css'

const API_URL =
  import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000'
const POLL_INTERVAL_MS = 1000
const PAGE_SIZE = 8

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

type CheckResult = {
  id: number
  url: string
  status: ResultStatus
  status_code: number | null
  response_time_ms: number | null
  title: string | null
  error: string | null
}

type JobAccepted = {
  job_id: string
  status: string
  total_urls: number
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
  results: CheckResult[]
}

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
        const response = await fetch(`${API_URL}/api/jobs?${params}`)
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
        const response = await fetch(`${API_URL}/api/jobs/${jobId}`)
        const responseData = await response.json()
        if (!response.ok) {
          throw new Error(responseData.detail ?? 'Failed to load job details.')
        }

        const detail = responseData as JobDetail
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
  }, [data?.job_id])

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    setSelectedFile(event.target.files?.[0] ?? null)
    setError('')
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedFile) {
      setError('Select a CSV file first.')
      return
    }

    const formData = new FormData()
    formData.append('file', selectedFile)
    setIsSubmitting(true)
    setError('')

    try {
      const response = await fetch(`${API_URL}/api/checks`, {
        method: 'POST',
        body: formData,
      })
      const responseData = await response.json()
      if (!response.ok) {
        throw new Error(responseData.detail ?? 'Failed to create the check job.')
      }

      const accepted = responseData as JobAccepted
      setData({
        job_id: accepted.job_id,
        filename: selectedFile.name,
        status: accepted.status,
        total: accepted.total_urls,
        completed: 0,
        successful: 0,
        failed: 0,
        error: null,
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

  async function runJobAction(
    action: 'retry-failed' | 'cancel',
    successMessage: string,
  ) {
    if (!data) return
    setActionName(action)
    try {
      const response = await fetch(
        `${API_URL}/api/jobs/${data.job_id}/${action}`,
        { method: 'POST' },
      )
      const responseData = await response.json()
      if (!response.ok) {
        throw new Error(responseData.detail ?? 'Action failed.')
      }
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
      const response = await fetch(
        `${API_URL}/api/jobs/${data.job_id}/retry-selected`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            result_ids: Array.from(selectedResultIds),
          }),
        },
      )
      const responseData = await response.json()
      if (!response.ok) {
        throw new Error(responseData.detail ?? 'Failed to retry selected results.')
      }

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
      const response = await fetch(`${API_URL}/api/jobs/${data.job_id}`, {
        method: 'DELETE',
      })
      const responseData = await response.json()
      if (!response.ok) {
        throw new Error(responseData.detail ?? 'Delete failed.')
      }
      setData(null)
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
    const average =
      completedResults.length > 0
        ? Math.round(
            completedResults.reduce(
              (sum, result) => sum + (result.response_time_ms ?? 0),
              0,
            ) / completedResults.length,
          )
        : 0
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
      average,
      slowest,
      blocked,
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

  const canRetry =
    data &&
    TERMINAL_STATUSES.has(data.status) &&
    data.results.some((result) => result.status !== 'healthy')
  const canCancel = data && ['queued', 'processing'].includes(data.status)
  const canDelete = data && TERMINAL_STATUSES.has(data.status)

  return (
    <main className="app-shell">
      <header className="site-header">
        <a className="brand" href="/">
          <span className="brand-mark">S</span>
          SitePulse
        </a>
        <span className="status">
          <span className="status-dot" />
          Local demo
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
          Check websites in bulk with live progress, performance summaries, and actionable failure categories.
        </p>
      </section>

      <section className="upload-card">
        <form onSubmit={handleSubmit}>
          <label className="file-picker">
            <span className="upload-icon">↑</span>
            <span className="file-title">
              {selectedFile ? selectedFile.name : 'Choose CSV file'}
            </span>
            <span className="file-help">Must contain a url column; up to 200 rows per job</span>
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={handleFileChange}
            />
          </label>
          <button className="check-button" type="submit" disabled={isSubmitting}>
            {isSubmitting ? 'Creating…' : 'Start checking'}
          </button>
        </form>
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
            </div>
            <div className="job-actions">
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

          {summary && (
            <section className="summary-grid">
              <article className="metric-card metric-primary">
                <span>Health rate</span>
                <strong>{summary.healthyRate}%</strong>
                <small>Based on completed results</small>
              </article>
              <article className="metric-card">
                <span>Average response</span>
                <strong>{summary.average || '—'}{summary.average ? ' ms' : ''}</strong>
                <small>All responding websites</small>
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
            </section>
          )}
        </>
      )}

      <section className="history-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">SAVED IN POSTGRESQL</p>
            <h2>Job history</h2>
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
          <div className="empty-state">No jobs match these filters.</div>
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
              <option value="server_error">Server error</option>
              <option value="timeout">Timeout</option>
              <option value="network_error">Network error</option>
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
                  <tr
                    key={result.id}
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
                    <td className="url-cell">{result.url}</td>
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
                      {result.status === 'queued'
                        ? 'Waiting for a worker'
                        : (result.error ?? result.title ?? '—')}
                    </td>
                  </tr>
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
