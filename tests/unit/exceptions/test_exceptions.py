from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapicommon.exceptions import (
    ConflictException,
    ForbiddenException,
    NotFoundException,
    PlatformException,
    UnauthorizedException,
    ValidationException,
    register_exception_handlers,
)


def test_platform_exception_defaults() -> None:
    exc = PlatformException()
    assert exc.status_code == 500
    assert exc.code == "internal_error"
    assert exc.message == exc.default_message


def test_platform_exception_custom_message() -> None:
    exc = NotFoundException("widget 123 not found")
    assert exc.code == "not_found"
    assert exc.message == "widget 123 not found"


def test_subclass_defaults() -> None:
    assert UnauthorizedException().status_code == 401
    assert ForbiddenException().status_code == 403
    assert ValidationException().status_code == 422
    assert ConflictException().status_code == 409
    assert NotFoundException().status_code == 404


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    class Item(BaseModel):
        name: str

    @app.get("/boom/{code}")
    async def boom(code: str) -> dict[str, str]:
        exceptions = {
            "unauthorized": UnauthorizedException(),
            "forbidden": ForbiddenException(),
            "not_found": NotFoundException(),
            "conflict": ConflictException(),
            "validation": ValidationException(),
        }
        if code == "unhandled":
            raise ValueError("boom")
        raise exceptions[code]

    @app.post("/items")
    async def create_item(item: Item) -> dict[str, str]:
        return {"name": item.name}

    return app


def test_platform_exception_handler_returns_structured_json() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    response = client.get("/boom/forbidden")
    assert response.status_code == 403
    body = response.json()
    # Matches Go gincommon's ErrorResponse shape exactly: {error, status, trace_id, request_id}
    # — no "code"/"message" keys on the wire (see PlatformException.code's docstring).
    assert set(body) == {"error", "status", "trace_id", "request_id"}
    assert body["error"] == "Access denied"
    assert body["status"] == 403
    assert isinstance(body["trace_id"], str)
    assert isinstance(body["request_id"], str)


def test_unhandled_exception_returns_500() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    response = client.get("/boom/unhandled")
    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"error", "status", "trace_id", "request_id"}
    assert body["status"] == 500


def test_request_validation_error_returns_422() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    response = client.post("/items", json={})
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error", "status", "trace_id", "request_id"}
    assert body["status"] == 422


def test_http_exception_passthrough_returns_404_for_unknown_route() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error", "status", "trace_id", "request_id"}
    assert body["status"] == 404


def test_error_response_carries_request_id_from_context_middleware() -> None:
    from fastapicommon.middleware import ContextMiddleware

    app = _build_app()
    app.add_middleware(ContextMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom/not_found", headers={"x-request-id": "req-abc123"})
    assert response.status_code == 404
    body = response.json()
    assert body["request_id"] == "req-abc123"
    assert response.headers["X-Request-ID"] == "req-abc123"
