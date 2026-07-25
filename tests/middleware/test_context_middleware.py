from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapicommon.context import get_request_context
from fastapicommon.middleware import ContextMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ContextMiddleware)

    @app.get("/whoami")
    async def whoami() -> dict[str, object]:
        context = get_request_context()
        return {
            "request_id": context.request_id,
            "correlation_id": context.correlation_id,
            "user_id": context.user_id,
            "tenant_id": context.tenant_id,
            "roles": list(context.roles),
        }

    return app


def test_context_extracted_from_headers() -> None:
    client = TestClient(_build_app())
    response = client.get(
        "/whoami",
        headers={
            "x-user-id": "u1",
            "x-tenant-id": "t1",
            "x-tenant-roles": "admin, operator",
            "x-request-id": "req-123",
            "x-correlation-id": "corr-456",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "request_id": "req-123",
        "correlation_id": "corr-456",
        "user_id": "u1",
        "tenant_id": "t1",
        "roles": ["admin", "operator"],
    }
    assert response.headers["x-request-id"] == "req-123"
    assert response.headers["x-correlation-id"] == "corr-456"


def test_ids_generated_when_absent() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami")
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["correlation_id"] == body["request_id"]
    assert body["user_id"] is None
    assert body["roles"] == []
    assert response.headers["x-request-id"] == body["request_id"]


def test_context_not_leaked_between_requests() -> None:
    client = TestClient(_build_app())
    first = client.get("/whoami", headers={"x-user-id": "u1"}).json()
    second = client.get("/whoami").json()
    assert first["user_id"] == "u1"
    assert second["user_id"] is None
    assert first["request_id"] != second["request_id"]
