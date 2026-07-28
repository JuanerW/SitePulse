import pytest
from fastapi import HTTPException

from main import MAX_URLS, read_urls_from_csv


def test_read_urls_from_csv_reads_and_deduplicates() -> None:
    content = (
        "url\n"
        "https://example.com\n"
        "https://github.com\n"
        "https://example.com\n"
    ).encode()

    assert read_urls_from_csv(content) == [
        "https://example.com",
        "https://github.com",
    ]


def test_read_urls_from_csv_accepts_utf8_bom() -> None:
    content = "url\nhttps://example.com\n".encode("utf-8-sig")

    assert read_urls_from_csv(content) == ["https://example.com"]


def test_read_urls_from_csv_requires_url_column() -> None:
    with pytest.raises(HTTPException) as exception:
        read_urls_from_csv(b"name\nexample\n")

    assert exception.value.status_code == 400
    assert "url" in str(exception.value.detail)


def test_read_urls_from_csv_rejects_empty_file() -> None:
    with pytest.raises(HTTPException) as exception:
        read_urls_from_csv(b"url\n")

    assert exception.value.status_code == 400


def test_read_urls_from_csv_enforces_batch_limit() -> None:
    rows = ["url", *[f"https://example.com/{index}" for index in range(MAX_URLS + 1)]]
    content = ("\n".join(rows) + "\n").encode()

    with pytest.raises(HTTPException) as exception:
        read_urls_from_csv(content)

    assert exception.value.status_code == 400
    assert str(MAX_URLS) in str(exception.value.detail)


def test_read_urls_from_csv_rejects_local_target() -> None:
    with pytest.raises(HTTPException) as exception:
        read_urls_from_csv(b"url\nhttp://localhost:8000\n")

    assert exception.value.status_code == 400
    assert "Local addresses" in str(exception.value.detail)
