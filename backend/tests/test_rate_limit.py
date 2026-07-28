from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

import rate_limit


class FakeRedis:
    def __init__(self, count: int) -> None:
        self.count = count

    async def incr(self, _: str) -> int:
        return self.count

    async def expire(self, _: str, __: int) -> None:
        return None


def build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/limited")
    async def limited(request: Request, response: Response) -> dict[str, bool]:
        await rate_limit.enforce_create_job_rate_limit(request, response)
        return {"ok": True}

    return app


def test_rate_limit_headers(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "redis_client", FakeRedis(count=3))
    response = TestClient(build_app()).post("/limited")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "7"


def test_rate_limit_returns_429(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "redis_client", FakeRedis(count=11))
    response = TestClient(build_app()).post("/limited")

    assert response.status_code == 429
    assert "Retry-After" in response.headers
