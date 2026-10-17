import asyncio
import ipaddress
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


REQUEST_TIMEOUT_SECONDS = 8.0
TOTAL_DEADLINE_SECONDS = 15.0
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
    final_url: str | None = None
    remote_ip: str | None = None
    redirect_count: int = 0


class UnsafeTargetError(ValueError):
    """The URL, or an address it resolves to, points at a non-public network."""


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
        raise UnsafeTargetError("Local addresses are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise UnsafeTargetError("Private, local, and reserved addresses are not allowed")

    return cleaned_url


async def resolve_public_addresses(url: str) -> list[str]:
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

    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise ValueError("Hostname did not resolve to an address")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeTargetError(
            "Hostname resolves to a private, local, or reserved address"
        )
    return addresses


def pinned_request(url: str, address: str) -> tuple[str, dict[str, str], dict]:
    """
    Build a request that connects to an already-validated IP address.

    Letting httpx resolve the hostname again would reopen a DNS-rebinding gap:
    the name could answer with a public IP during validation and a private one
    at connect time. The URL carries the IP; the Host header and TLS SNI keep
    the original name, so virtual hosting and certificate checks still work.
    """

    parsed = urlparse(url)
    assert parsed.hostname is not None
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    target = parsed._replace(netloc=netloc).geturl()
    headers = {"Host": parsed.netloc}
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}
    return target, headers, extensions


@asynccontextmanager
async def pinned_stream(
    client: httpx.AsyncClient,
    url: str,
    addresses: list[str],
    attempted: list[str] | None = None,
) -> AsyncIterator[tuple[str, httpx.Response]]:
    """
    Stream a GET from the first validated address that accepts a connection.

    IPv4 goes first: many container networks have no IPv6 route, and a name
    with both record types would otherwise fail on the AAAA answer alone.
    Only connection failures move on to the next address; a timeout or an
    HTTP error is the site's answer and is reported as-is.
    """

    ordered = sorted(addresses, key=lambda address: ":" in address)
    last_error: httpx.ConnectError | None = None
    for address in ordered:
        if attempted is not None:
            # Recorded before sending, so a timeout waiting for headers still
            # reports which address it was talking to.
            attempted.append(address)
        target, headers, extensions = pinned_request(url, address)
        request = client.build_request(
            "GET", target, headers=headers, extensions=extensions
        )
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except httpx.ConnectError as exc:
            last_error = exc
            continue
        try:
            yield address, response
        finally:
            await response.aclose()
        return
    assert last_error is not None
    raise last_error


async def check_one_website(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    raw_url: str,
) -> WebsiteCheck:
    """Check one URL and map common network failures to product results."""

    try:
        url = validate_url(raw_url)
    except UnsafeTargetError as exc:
        return WebsiteCheck(url=raw_url, status="unsafe_target", error=str(exc))
    except ValueError as exc:
        return WebsiteCheck(url=raw_url, status="network_error", error=str(exc))

    # Kept outside the try so failed checks still report how far they got.
    current_url = url
    remote_ip: str | None = None
    redirect_count = 0
    attempted: list[str] = []
    started_at = time.perf_counter()

    def outcome(status: str, error: str | None = None, **fields: object) -> WebsiteCheck:
        return WebsiteCheck(
            url=url,
            status=status,
            error=error,
            final_url=current_url,
            remote_ip=remote_ip,
            redirect_count=redirect_count,
            **fields,  # type: ignore[arg-type]
        )

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started_at) * 1000)

    try:
        async with semaphore:
            started_at = time.perf_counter()
            response: httpx.Response | None = None
            body = b""
            # httpx's timeout applies per network operation, so a server that
            # drips one byte every few seconds would never trip it. This caps
            # the whole check, redirects included.
            async with asyncio.timeout(TOTAL_DEADLINE_SECONDS):
                for redirect_number in range(MAX_REDIRECTS + 1):
                    addresses = await resolve_public_addresses(current_url)
                    async with pinned_stream(
                        client, current_url, addresses, attempted
                    ) as (
                        connected_ip,
                        streamed_response,
                    ):
                        remote_ip = connected_ip
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
                            # Join against the hostname URL, not the pinned IP URL.
                            current_url = urljoin(current_url, location)
                            redirect_count += 1
                            validate_url(current_url)
                            continue

                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                            return outcome(
                                "response_too_large",
                                f"Response exceeded {MAX_RESPONSE_BYTES} bytes",
                                status_code=response.status_code,
                            )
                        chunks: list[bytes] = []
                        received = 0
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > MAX_RESPONSE_BYTES:
                                return outcome(
                                    "response_too_large",
                                    f"Response exceeded {MAX_RESPONSE_BYTES} bytes",
                                    status_code=response.status_code,
                                )
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        break

        assert response is not None
        parser = TitleParser()
        if "text/html" in response.headers.get("content-type", ""):
            parser.feed(body.decode(response.encoding or "utf-8", errors="replace"))

        result_status = classify_status(response.status_code)
        return outcome(
            result_status,
            None if result_status == "healthy" else f"HTTP {response.status_code}",
            status_code=response.status_code,
            response_time_ms=elapsed_ms(),
            title=parser.title,
        )
    except (httpx.TimeoutException, TimeoutError):
        remote_ip = remote_ip or (attempted[-1] if attempted else None)
        return outcome(
            "timeout",
            f"No complete answer after {elapsed_ms() / 1000:.1f} s",
            response_time_ms=elapsed_ms(),
        )
    except httpx.RequestError as exc:
        return outcome("network_error", f"Network error: {exc}")
    except UnsafeTargetError as exc:
        # Raised for the first hop or for any redirect target.
        if redirect_count:
            host = urlparse(current_url).hostname
            return outcome(
                "unsafe_target",
                f"Redirect {redirect_count} pointed at {host}, refused: {exc}",
            )
        return outcome("unsafe_target", str(exc))
    except ValueError as exc:
        return outcome("network_error", str(exc))
