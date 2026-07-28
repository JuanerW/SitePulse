import asyncio

import httpx
import pytest

from checker import check_one_website, classify_status, validate_url


def test_validate_url_accepts_http_and_https() -> None:
    assert validate_url("https://example.com") == "https://example.com"
    assert validate_url(" http://example.com/path ") == "http://example.com/path"


@pytest.mark.parametrize(
    ("url", "expected_message"),
    [
        ("ftp://example.com", "http://"),
        ("not-a-url", "http://"),
        ("http://localhost:8000", "Local addresses"),
        ("http://127.0.0.1", "Local addresses"),
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
        assert request.url == "https://example.com"
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
