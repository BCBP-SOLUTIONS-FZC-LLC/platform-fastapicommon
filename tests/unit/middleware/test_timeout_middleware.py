import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapicommon.middleware import TimeoutMiddleware


def _build_app(timeout_seconds: float) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TimeoutMiddleware, timeout_seconds=timeout_seconds)

    @app.get("/fast")
    async def fast() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await anyio.sleep(0.2)
        return {"status": "ok"}

    return app


def test_fast_request_completes_normally() -> None:
    client = TestClient(_build_app(timeout_seconds=1.0))
    response = client.get("/fast")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_slow_request_returns_503_problem() -> None:
    # Same response as gincommon's TimeoutMiddleware (RFC 9457 parity; it was a 504).
    client = TestClient(_build_app(timeout_seconds=0.05))
    response = client.get("/slow")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "https://errors.company.com/platform/service-unavailable"
    assert body["status"] == 503
    assert body["errorCode"] == "REQUEST_TIMEOUT"
    assert body["detail"] == "The request timed out."
    assert body["instance"] == "/slow" and body["traceId"]
