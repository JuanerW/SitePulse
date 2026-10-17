export const API_URL = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000'

// The sample job checks SitePulse's own demo endpoints through the public
// hostname, so it works from any deployment that can reach the internet.
const DEMO_ORIGIN = import.meta.env.VITE_DEMO_ORIGIN ?? 'https://sitepulse.hanyuuu.com'

const OWNER_STORAGE_KEY = 'sitepulse-owner-token'
let memoryOwnerToken: string | null = null

function randomToken() {
  const bytes = new Uint8Array(24)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * A random token that marks this browser as the owner of the jobs it starts.
 * There are no accounts: history, cancel, retry and delete only work for the
 * browser holding the token. Private windows fall back to an in-memory token.
 */
export function ownerToken(): string {
  try {
    const stored = localStorage.getItem(OWNER_STORAGE_KEY)
    if (stored) return stored
    const created = randomToken()
    localStorage.setItem(OWNER_STORAGE_KEY, created)
    return created
  } catch {
    memoryOwnerToken ??= randomToken()
    return memoryOwnerToken
  }
}

export function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  headers.set('X-Owner-Token', ownerToken())
  return fetch(`${API_URL}${path}`, { ...init, headers })
}

export function newIdempotencyKey() {
  return `web-${randomToken().slice(0, 32)}`
}

export type SampleUrl = { url: string; expect: string }

export const SAMPLE_URLS: SampleUrl[] = [
  { url: 'https://example.com', expect: 'healthy' },
  { url: 'https://www.wikipedia.org', expect: 'healthy' },
  { url: 'https://github.com', expect: 'healthy' },
  { url: `${DEMO_ORIGIN}/api/demo/redirect`, expect: 'healthy after 1 redirect' },
  { url: `${DEMO_ORIGIN}/api/demo/status/403`, expect: 'blocked' },
  { url: `${DEMO_ORIGIN}/api/demo/status/404`, expect: 'not found' },
  { url: `${DEMO_ORIGIN}/api/demo/status/429`, expect: 'rate limited' },
  { url: `${DEMO_ORIGIN}/api/demo/status/500`, expect: 'server error' },
  { url: `${DEMO_ORIGIN}/api/demo/slow`, expect: 'timeout after 8 s' },
  { url: 'https://sitepulse-demo.invalid', expect: 'network error (no DNS)' },
  { url: 'http://127.0.0.1.nip.io', expect: 'unsafe: DNS answers 127.0.0.1' },
  {
    url: `${DEMO_ORIGIN}/api/demo/redirect-to-metadata`,
    expect: 'unsafe: redirects to 169.254.169.254',
  },
]

function csvField(value: string) {
  return /[",\r\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value
}

export function urlsToCsv(urls: string[]) {
  return ['url', ...urls.map(csvField)].join('\n') + '\n'
}

export function urlsToCsvFile(urls: string[], filename: string) {
  return new File([urlsToCsv(urls)], filename, { type: 'text/csv' })
}

/**
 * Turn pasted text into URLs: one per line (commas and spaces also work),
 * a "url" header row is ignored, and a bare domain gets https://.
 */
export function parsePastedUrls(text: string): string[] {
  const urls = text
    .split(/[\s,]+/)
    .map((part) => part.trim().replace(/^["']|["']$/g, ''))
    .filter((part) => part && part.toLowerCase() !== 'url')
    .map((part) => (/^[a-z][a-z0-9+.-]*:\/\//i.test(part) ? part : `https://${part}`))
  return Array.from(new Set(urls))
}
