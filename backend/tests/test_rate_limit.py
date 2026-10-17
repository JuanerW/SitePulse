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


def build_ip_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ip")
    async def ip(request: Request) -> dict[str, str]:
        return {"ip": rate_limit.client_ip(request)}

    return app


def test_client_ip_trusts_x_real_ip_only_from_private_proxy() -> None:
    proxied = TestClient(build_ip_app(), client=("172.18.0.5", 50000))
    assert proxied.get("/ip", headers={"X-Real-IP": "203.0.113.9"}).json() == {
        "ip": "203.0.113.9"
    }

    direct = TestClient(build_ip_app(), client=("8.8.8.8", 50000))
    assert direct.get("/ip", headers={"X-Real-IP": "203.0.113.9"}).json() == {
        "ip": "8.8.8.8"
    }


def test_ipv6_visitors_share_a_bucket_per_slash_64() -> None:
    a = rate_limit.rate_limit_subject("2001:db8:1:2::1")
    b = rate_limit.rate_limit_subject("2001:db8:1:2:ffff::9")
    assert a == b == "2001:db8:1:2::/64"
    assert rate_limit.rate_limit_subject("2001:db8:1:3::1") != a
    assert rate_limit.rate_limit_subject("203.0.113.9") == "203.0.113.9"
