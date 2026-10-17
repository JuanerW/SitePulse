"""HTTP + PostgreSQL integration tests for the public job API."""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from main import app
from rate_limit import enforce_retry_rate_limit


async def disable_rate_limit(*_: object) -> None:
    return None


def no_limit() -> None:
    return None


def owner_headers(token: str) -> dict[str, str]:
    return {"X-Owner-Token": token}


def test_create_read_idempotent_replay_and_delete_job(monkeypatch) -> None:
    monkeypatch.setattr(main, "enforce_create_job_rate_limit", disable_rate_limit)
    owner = owner_headers(f"owner-{uuid.uuid4()}")
    key = f"integration-{uuid.uuid4()}"
    csv_content = b"url\nhttps://example.com\nhttps://example.org\n"

    try:
        with patch("main.check_job.delay") as delay:
            with TestClient(app) as client:
                created = client.post(
                    "/api/checks",
                    files={"file": ("integration.csv", csv_content, "text/csv")},
                    headers={"Idempotency-Key": key, **owner},
                )
                assert created.status_code == 202
                job = created.json()
                assert job["total_urls"] == 2
                delay.assert_called_once()

                replay = client.post(
                    "/api/checks",
                    files={"file": ("integration.csv", csv_content, "text/csv")},
                    headers={"Idempotency-Key": key, **owner},
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
                    headers={"Idempotency-Key": key, **owner},
                )
                assert conflict.status_code == 409

                assert replay.json()["replayed"] is True
                assert detail.json()["is_owner"] is False  # no token sent
                assert client.post(f"/api/jobs/{job['job_id']}/cancel", headers=owner).status_code == 200
                assert client.delete(f"/api/jobs/{job['job_id']}", headers=owner).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_upload_size_limit_returns_413(monkeypatch) -> None:
    monkeypatch.setattr(main, "enforce_create_job_rate_limit", disable_rate_limit)
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


def test_jobs_are_private_to_the_browser_that_created_them(monkeypatch) -> None:
    monkeypatch.setattr(main, "enforce_create_job_rate_limit", disable_rate_limit)
    app.dependency_overrides[enforce_retry_rate_limit] = no_limit
    alice = owner_headers(f"alice-{uuid.uuid4()}")
    mallory = owner_headers(f"mallory-{uuid.uuid4()}")

    try:
        with patch("main.check_job.delay"):
            with TestClient(app) as client:
                created = client.post(
                    "/api/checks",
                    files={"file": ("private.csv", b"url\nhttps://example.com\n", "text/csv")},
                    headers=alice,
                )
                job_id = created.json()["job_id"]

                mine = client.get("/api/jobs", headers=alice).json()
                assert [item["id"] for item in mine["items"]] == [job_id]
                assert client.get("/api/jobs", headers=mallory).json()["total"] == 0
                assert client.get("/api/jobs").json()["total"] == 0

                # A shared link is readable, but only the owner may act on it.
                shared = client.get(f"/api/jobs/{job_id}", headers=mallory)
                assert shared.status_code == 200
                assert shared.json()["is_owner"] is False
                assert client.get(f"/api/jobs/{job_id}", headers=alice).json()["is_owner"] is True
                assert client.post(f"/api/jobs/{job_id}/cancel", headers=mallory).status_code == 403
                assert client.delete(f"/api/jobs/{job_id}", headers=mallory).status_code == 403
                assert client.post(f"/api/jobs/{job_id}/retry-failed", headers=mallory).status_code == 403

                assert client.post(f"/api/jobs/{job_id}/cancel", headers=alice).status_code == 200
                assert client.delete(f"/api/jobs/{job_id}", headers=alice).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_idempotent_replay_does_not_spend_rate_limit(monkeypatch) -> None:
    calls: list[int] = []

    async def counting_limit(*_: object) -> None:
        calls.append(1)

    monkeypatch.setattr(main, "enforce_create_job_rate_limit", counting_limit)
    owner = owner_headers(f"owner-{uuid.uuid4()}")
    headers = {"Idempotency-Key": f"quota-{uuid.uuid4()}", **owner}
    upload = {"file": ("q.csv", b"url\nhttps://example.com\n", "text/csv")}

    with patch("main.check_job.delay"):
        with TestClient(app) as client:
            first = client.post("/api/checks", files=upload, headers=headers)
            second = client.post("/api/checks", files=upload, headers=headers)
            assert first.json()["job_id"] == second.json()["job_id"]
            client.post(f"/api/jobs/{first.json()['job_id']}/cancel", headers=owner)

    assert len(calls) == 1
