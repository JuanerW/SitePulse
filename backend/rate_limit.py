"""Redis-backed fixed-window limits for expensive API operations."""

import logging
import os
import time

from fastapi import HTTPException, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CREATE_JOB_LIMIT = int(os.getenv("CREATE_JOB_RATE_LIMIT", "10"))
WINDOW_SECONDS = 60
logger = logging.getLogger("sitepulse.api")
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


async def enforce_create_job_rate_limit(
    request: Request,
    response: Response,
) -> None:
    """Allow a bounded number of new jobs per client IP and minute."""

    client_ip = request.client.host if request.client else "unknown"
    window = int(time.time() // WINDOW_SECONDS)
    key = f"sitepulse:rate:create:{client_ip}:{window}"

    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, WINDOW_SECONDS + 1)
    except RedisError:
        # Job submission will independently report Redis queue failures. A
        # limiter outage should not hide an otherwise healthy API.
        logger.exception("rate_limiter_unavailable")
        return

    remaining = max(0, CREATE_JOB_LIMIT - count)
    response.headers["X-RateLimit-Limit"] = str(CREATE_JOB_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if count > CREATE_JOB_LIMIT:
        retry_after = WINDOW_SECONDS - (int(time.time()) % WINDOW_SECONDS)
        raise HTTPException(
            status_code=429,
            detail="Too many check jobs; try again shortly",
            headers={"Retry-After": str(retry_after)},
        )
