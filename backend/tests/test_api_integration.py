"""HTTP + PostgreSQL integration tests for the public job API."""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from rate_limit import enforce_create_job_rate_limit


def disable_rate_limit() -> None:
    return None


def test_create_read_idempotent_replay_and_delete_job() -> None:
    app.dependency_overrides[enforce_create_job_rate_limit] = disable_rate_limit
    key = f"integration-{uuid.uuid4()}"
    csv_content = b"url\nhttps://example.com\nhttps://example.org\n"

    try:
        with patch("main.check_job.delay") as delay:
            with TestClient(app) as client:
                created = client.post(
                    "/api/checks",
                    files={"file": ("integration.csv", csv_content, "text/csv")},
                    headers={"Idempotency-Key": key},
                )
                assert created.status_code == 202
                job = created.json()
                assert job["total_urls"] == 2
                delay.assert_called_once()

                replay = client.post(
                    "/api/checks",
                    files={"file": ("integration.csv", csv_content, "text/csv")},
                    headers={"Idempotency-Key": key},
                )
                assert replay.status_code == 202
                assert replay.json()["job_id"] == job["job_id"]
                delay.assert_called_once()

                detail = client.get(f"/api/jobs/{job['job_id']}")
                assert detail.status_code == 200
                assert len(detail.json()["results"]) == 2
                assert all(
                    isinstance(result["id"], int)
                    for result in detail.json()["results"]
                )

                conflict = client.post(
                    "/api/checks",
                    files={
                        "file": (
                            "different.csv",
                            b"url\nhttps://www.python.org\n",
                            "text/csv",
                        )
                    },
                    headers={"Idempotency-Key": key},
                )
                assert conflict.status_code == 409

                assert client.post(f"/api/jobs/{job['job_id']}/cancel").status_code == 200
                assert client.delete(f"/api/jobs/{job['job_id']}").status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_upload_size_limit_returns_413() -> None:
    app.dependency_overrides[enforce_create_job_rate_limit] = disable_rate_limit
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/checks",
                files={
                    "file": (
                        "oversized.csv",
                        b"url\n" + b"x" * 1_000_001,
                        "text/csv",
                    )
                },
            )
        assert response.status_code == 413
    finally:
        app.dependency_overrides.clear()
