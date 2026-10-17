"""Redis-backed fixed-window limits for expensive API operations."""

import ipaddress
import logging
import os
import time

from fastapi import HTTPException, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CREATE_JOB_LIMIT = int(os.getenv("CREATE_JOB_RATE_LIMIT", "10"))
RETRY_JOB_LIMIT = int(os.getenv("RETRY_JOB_RATE_LIMIT", "10"))
WINDOW_SECONDS = 60
logger = logging.getLogger("sitepulse.api")
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


def client_ip(request: Request) -> str:
    """
    Return the visitor's address, trusting X-Real-IP only from a private peer.

    In production the API is reached through the frontend nginx container, so
    the TCP peer is always that container. nginx rewrites X-Real-IP from the
    address the edge proxy reported, and nothing outside the Docker network
    can connect to the API directly. A public peer is a direct client, and its
    own X-Real-IP header is ignored so it cannot pick its rate-limit bucket.
    """

    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-real-ip", "").strip()
    try:
        peer_is_private = not ipaddress.ip_address(peer).is_global
    except ValueError:
        peer_is_private = False
    if peer_is_private and forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


async def enforce_rate_limit(
    request: Request,
    response: Response,
    bucket: str,
    limit: int,
) -> None:
    """Allow `limit` operations per client IP and minute in one bucket."""

    window = int(time.time() // WINDOW_SECONDS)
    key = f"sitepulse:rate:{bucket}:{client_ip(request)}:{window}"

    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, WINDOW_SECONDS + 1)
    except RedisError:
        # Job submission will independently report Redis queue failures. A
        # limiter outage should not hide an otherwise healthy API.
        logger.exception("rate_limiter_unavailable")
        return

    remaining = max(0, limit - count)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if count > limit:
        retry_after = WINDOW_SECONDS - (int(time.time()) % WINDOW_SECONDS)
        logger.info("rate_limited", extra={"bucket": bucket})
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests; try again in {retry_after} seconds",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )


async def enforce_create_job_rate_limit(
    request: Request,
    response: Response,
) -> None:
    """Allow a bounded number of new jobs per client IP and minute."""

    await enforce_rate_limit(request, response, "create", CREATE_JOB_LIMIT)


async def enforce_retry_rate_limit(
    request: Request,
    response: Response,
) -> None:
    """Retries re-run checks too, so they get their own per-IP budget."""

    await enforce_rate_limit(request, response, "retry", RETRY_JOB_LIMIT)
