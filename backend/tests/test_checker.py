import asyncio

import httpx
import pytest

import checker
from checker import check_one_website, classify_status, validate_url


@pytest.fixture(autouse=True)
def avoid_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public_dns(_: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(checker, "resolve_public_addresses", public_dns)


def test_validate_url_accepts_http_and_https() -> None:
    assert validate_url("https://example.com") == "https://example.com"
    assert validate_url(" http://example.com/path ") == "http://example.com/path"


@pytest.mark.parametrize(
    ("url", "expected_message"),
    [
        ("ftp://example.com", "http://"),
        ("not-a-url", "http://"),
        ("http://localhost:8000", "Local addresses"),
        ("http://127.0.0.1", "Private"),
        ("http://10.0.0.1", "Private"),
        ("http://169.254.169.254/latest/meta-data", "Private"),
        ("http://user:password@example.com", "credentials"),
    ],
)
def test_validate_url_rejects_invalid_or_local_addresses(
    url: str,
    expected_message: str,
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        validate_url(url)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, "healthy"),
        (302, "healthy"),
        (403, "blocked"),
        (404, "not_found"),
        (429, "rate_limited"),
        (503, "server_error"),
        (418, "client_error"),
    ],
)
def test_classify_status(status_code: int, expected: str) -> None:
    assert classify_status(status_code) == expected


@pytest.mark.anyio
async def test_check_one_website_returns_success_and_title() -> None:
    """Use MockTransport for a deterministic response without real network I/O."""

    def handler(request: httpx.Request) -> httpx.Response:
        # The connection goes to the validated IP; Host and SNI keep the name.
        assert request.url == "https://93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><title>Example Test</title></head></html>",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await check_one_website(
            client,
            asyncio.Semaphore(1),
            "https://example.com",
        )

    assert result.status == "healthy"
    assert result.status_code == 200
    assert result.title == "Example Test"
    assert result.response_time_ms is not None
    assert result.error is None


@pytest.mark.anyio
async def test_check_one_website_keeps_http_error_as_result() -> None:
    """A 404 is a URL result and must not crash the entire background job."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not found")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await check_one_website(
            client,
            asyncio.Semaphore(1),
            "https://example.com/missing",
        )

    assert result.status == "not_found"
    assert result.status_code == 404
    assert result.error == "HTTP 404"


@pytest.mark.anyio
async def test_check_rejects_oversized_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(checker.MAX_RESPONSE_BYTES + 1)},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_one_website(
            client, asyncio.Semaphore(1), "https://example.com"
        )

    assert result.status == "response_too_large"


@pytest.mark.anyio
async def test_connects_to_the_address_that_was_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS rebinding: a second lookup answering 127.0.0.1 must never be used."""

    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def rebinding_dns(_: str) -> list[str]:
        return next(answers)

    monkeypatch.setattr(checker, "resolve_public_addresses", rebinding_dns)
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_one_website(
            client, asyncio.Semaphore(1), "http://rebind.example"
        )

    assert seen_hosts == ["93.184.216.34"]
    assert result.status == "healthy"
    assert result.remote_ip == "93.184.216.34"


@pytest.mark.anyio
async def test_redirect_to_private_address_is_unsafe_target() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_one_website(
            client, asyncio.Semaphore(1), "https://example.com/redirect"
        )

    assert result.status == "unsafe_target"
    assert "Private" in (result.error or "")


@pytest.mark.anyio
async def test_hostname_resolving_to_private_address_is_unsafe_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()

    def private_dns(*_: object, **__: object) -> list[tuple]:
        return [(None, None, None, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(checker.socket, "getaddrinfo", private_dns)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        result = await check_one_website(
            client, asyncio.Semaphore(1), "http://127.0.0.1.nip.io"
        )

    assert result.status == "unsafe_target"


@pytest.mark.anyio
async def test_redirect_hops_and_final_url_are_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_one_website(
            client, asyncio.Semaphore(1), "https://example.com/old"
        )

    assert result.status == "healthy"
    assert result.redirect_count == 1
    assert result.final_url == "https://example.com/new"
