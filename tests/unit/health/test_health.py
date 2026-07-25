from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapicommon.health import create_health_router, health_router


def test_default_health_router() -> None:
    app = FastAPI()
    app.include_router(health_router)
    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/live").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ok"}


def test_ready_returns_503_when_sync_check_fails() -> None:
    app = FastAPI()
    app.include_router(create_health_router(readiness_checks=[lambda: False]))
    client = TestClient(app)

    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


async def _async_check_ok() -> bool:
    return True


def test_ready_supports_async_checks() -> None:
    app = FastAPI()
    app.include_router(create_health_router(readiness_checks=[_async_check_ok]))
    client = TestClient(app)

    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
