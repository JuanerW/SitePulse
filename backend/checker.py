import asyncio
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx


REQUEST_TIMEOUT_SECONDS = 8.0
MAX_CONCURRENT_CHECKS = 10


@dataclass
class WebsiteCheck:
    """Database-independent result for one website check."""

    url: str
    status: str
    status_code: int | None = None
    response_time_ms: int | None = None
    title: str | None = None
    error: str | None = None


def classify_status(status_code: int) -> str:
    """Map raw HTTP status codes to user-facing product statuses."""

    if 200 <= status_code < 400:
        return "healthy"
    if status_code in {401, 403}:
        return "blocked"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code < 600:
        return "server_error"
    return "client_error"


class TitleParser(HTMLParser):
    """Extract the HTML title with the standard library."""

    def __init__(self) -> None:
        super().__init__()
        self.inside_title = False
        self.title_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "title":
            self.inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.inside_title = False

    def handle_data(self, data: str) -> None:
        if self.inside_title:
            self.title_parts.append(data.strip())

    @property
    def title(self) -> str | None:
        value = " ".join(part for part in self.title_parts if part).strip()
        return value[:200] or None


def validate_url(url: str) -> str:
    """Basic URL validation; complete DNS/IP SSRF protection is still planned."""

    cleaned_url = url.strip()
    parsed = urlparse(cleaned_url)

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must start with http:// or https://")

    if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local addresses are not allowed")

    return cleaned_url


async def check_one_website(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    raw_url: str,
) -> WebsiteCheck:
    """Check one URL and map common network failures to product results."""

    try:
        url = validate_url(raw_url)
    except ValueError as exc:
        return WebsiteCheck(url=raw_url, status="failed", error=str(exc))

    try:
        async with semaphore:
            started_at = time.perf_counter()
            response = await client.get(url)
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)

        parser = TitleParser()
        if "text/html" in response.headers.get("content-type", ""):
            parser.feed(response.text)

        result_status = classify_status(response.status_code)
        return WebsiteCheck(
            url=url,
            status=result_status,
            status_code=response.status_code,
            response_time_ms=elapsed_ms,
            title=parser.title,
            error=None if result_status == "healthy" else f"HTTP {response.status_code}",
        )
    except httpx.TimeoutException:
        return WebsiteCheck(url=url, status="timeout", error="Request timed out")
    except httpx.RequestError as exc:
        return WebsiteCheck(
            url=url,
            status="network_error",
            error=f"Network error: {exc}",
        )
