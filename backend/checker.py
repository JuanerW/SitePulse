import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


REQUEST_TIMEOUT_SECONDS = 8.0
MAX_CONCURRENT_CHECKS = 10
MAX_RESPONSE_BYTES = 1_000_000
MAX_REDIRECTS = 5


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
    """Validate URL syntax and reject immediately identifiable private targets."""

    cleaned_url = url.strip()
    parsed = urlparse(cleaned_url)

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Local addresses are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private, local, and reserved addresses are not allowed")

    return cleaned_url


async def resolve_public_addresses(url: str) -> set[str]:
    """Resolve a hostname and reject every non-public IPv4 or IPv6 address."""

    parsed = urlparse(validate_url(url))
    assert parsed.hostname is not None
    loop = asyncio.get_running_loop()
    try:
        records = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
        )
    except socket.gaierror as exc:
        raise ValueError("Hostname could not be resolved") from exc

    addresses = {record[4][0] for record in records}
    if not addresses:
        raise ValueError("Hostname did not resolve to an address")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Hostname resolves to a private, local, or reserved address")
    return addresses


async def check_one_website(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    raw_url: str,
) -> WebsiteCheck:
    """Check one URL and map common network failures to product results."""

    try:
        url = validate_url(raw_url)
        await resolve_public_addresses(url)
    except ValueError as exc:
        return WebsiteCheck(url=raw_url, status="failed", error=str(exc))

    try:
        async with semaphore:
            started_at = time.perf_counter()
            response: httpx.Response | None = None
            body = b""
            current_url = url
            for redirect_number in range(MAX_REDIRECTS + 1):
                await resolve_public_addresses(current_url)
                async with client.stream(
                    "GET",
                    current_url,
                    follow_redirects=False,
                ) as streamed_response:
                    response = streamed_response
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        if redirect_number == MAX_REDIRECTS:
                            raise httpx.TooManyRedirects(
                                "Maximum redirect count exceeded",
                                request=response.request,
                            )
                        current_url = urljoin(str(response.url), location)
                        validate_url(current_url)
                        continue

                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                        return WebsiteCheck(
                            url=url,
                            status="response_too_large",
                            status_code=response.status_code,
                            error=f"Response exceeded {MAX_RESPONSE_BYTES} bytes",
                        )
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > MAX_RESPONSE_BYTES:
                            return WebsiteCheck(
                                url=url,
                                status="response_too_large",
                                status_code=response.status_code,
                                error=f"Response exceeded {MAX_RESPONSE_BYTES} bytes",
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    break
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)

        assert response is not None
        parser = TitleParser()
        if "text/html" in response.headers.get("content-type", ""):
            parser.feed(body.decode(response.encoding or "utf-8", errors="replace"))

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
    except ValueError as exc:
        return WebsiteCheck(url=url, status="network_error", error=str(exc))
