import io
import json
import logging
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability import (
    JsonFormatter,
    RequestContextMiddleware,
    correlation_context,
)


def build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_request_id_header_is_preserved() -> None:
    client = TestClient(build_test_app())

    response = client.get("/ping", headers={"X-Request-ID": "test-request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request-123"


def test_request_id_is_generated_when_missing() -> None:
    client = TestClient(build_test_app())

    response = client.get("/ping")

    uuid.UUID(response.headers["X-Request-ID"])


def test_json_logs_include_correlation_ids_and_extra_fields() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("sitepulse.test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with correlation_context(request_id="req-1", job_id="job-2"):
        logger.info("check_completed", extra={"status_code": 200})

    payload = json.loads(output.getvalue())
    assert payload["event"] == "check_completed"
    assert payload["request_id"] == "req-1"
    assert payload["job_id"] == "job-2"
    assert payload["status_code"] == 200
