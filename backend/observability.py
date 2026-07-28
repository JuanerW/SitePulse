"""Shared JSON logging and correlation context for the API and workers."""

import json
import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from time import perf_counter
from typing import Iterator

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


request_id_context: ContextVar[str | None] = ContextVar(
    "request_id",
    default=None,
)
job_id_context: ContextVar[str | None] = ContextVar("job_id", default=None)

_STANDARD_LOG_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Serialize one log record per line for log aggregation systems."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }

        request_id = request_id_context.get()
        job_id = job_id_context.get()
        if request_id:
            payload["request_id"] = request_id
        if job_id:
            payload["job_id"] = job_id

        # `extra={...}` fields become regular LogRecord attributes. Preserve
        # them as structured values instead of embedding them in the message.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_FIELDS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the shared JSON handler for application loggers."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level.upper())


def current_request_id() -> str | None:
    """Return the request ID currently bound to this execution context."""

    return request_id_context.get()


@contextmanager
def correlation_context(
    *,
    request_id: str | None = None,
    job_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation IDs and always restore the previous context."""

    tokens: list[tuple[ContextVar[str | None], Token]] = []
    if request_id:
        tokens.append((request_id_context, request_id_context.set(request_id)))
    if job_id:
        tokens.append((job_id_context, job_id_context.set(job_id)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request ID to responses and all logs emitted by the request."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied_id = request.headers.get("X-Request-ID", "").strip()
        request_id = supplied_id[:128] if supplied_id else str(uuid.uuid4())
        started_at = perf_counter()
        logger = logging.getLogger("sitepulse.api")

        with correlation_context(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "request_failed",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": round((perf_counter() - started_at) * 1000),
                    },
                )
                raise

            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request_completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started_at) * 1000),
                },
            )
            return response
