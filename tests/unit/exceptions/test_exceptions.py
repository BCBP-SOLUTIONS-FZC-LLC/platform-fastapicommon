from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel, Field

from fastapicommon.exceptions import (
    BusinessException,
    BusinessRuleViolationException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
    PlatformException,
    RateLimitException,
    ResourceNotFoundException,
    ServiceUnavailableException,
    UnauthorizedException,
    UnsupportedOperationException,
    ValidationException,
    register_exception_handlers,
)
from fastapicommon.middleware import ContextMiddleware
from fastapicommon.problem import (
    PROBLEM_MEDIA_TYPE,
    TYPE_VALIDATION_FAILED,
    FieldError,
    problem_type,
)

TENDER_NOT_FOUND = problem_type("tender", "not-found", "Tender not found", 404, "TENDER_NOT_FOUND")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class Item(BaseModel):
    sku: str = Field(max_length=8)


class CreateTender(BaseModel):
    title: str
    deadline: datetime
    budget: int = Field(default=1, ge=1)
    stage: Literal["draft", "open"] | None = None
    items: list[Item] = []


def _build_app(*, context: bool = True, **kwargs: str) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app, **kwargs)
    if context:
        app.add_middleware(ContextMiddleware)

    @app.get("/boom/{code}")
    async def boom(code: str) -> dict[str, str]:
        exceptions: dict[str, Exception] = {
            "unauthorized": UnauthorizedException(),
            "forbidden": ForbiddenException(),
            "not_found": NotFoundException("Widget 7 was not found."),
            "conflict": ConflictException(),
            "validation": ValidationException(
                validation_errors=[FieldError("deadline", "must be in the future")]
            ),
            "rule": BusinessRuleViolationException("Bids are closed."),
            "rate": RateLimitException(),
            "unsupported": UnsupportedOperationException(),
            "unavailable": ServiceUnavailableException("The database is unavailable."),
            "tender": PlatformException("Tender 123 was not found.", problem_type=TENDER_NOT_FOUND),
            "secret500": PlatformException("pq: password=hunter2"),
            "unhandled": ValueError("pq: duplicate key (alice@example.com)"),
            "notimpl": NotImplementedError("x"),
            "http409": HTTPException(409, "Version mismatch.", headers={"Retry-After": "1"}),
            "http500": HTTPException(500, "secret"),
        }
        raise exceptions[code]

    @app.post("/tenders")
    async def create(t: CreateTender) -> dict[str, str]:
        return {"title": t.title}

    @app.put("/only-put")
    async def only_put() -> dict[str, str]:
        return {}

    return app


def _problem(resp: Response, status: int, type_id: str, code: str) -> dict[str, object]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = resp.json()
    for k in ("type", "title", "status", "instance", "traceId", "errorCode", "timestamp"):
        assert k in body, (k, body)
    assert body["type"] == f"https://errors.company.com/{type_id}"
    assert body["status"] == status
    assert body["errorCode"] == code
    assert _TS.match(str(body["timestamp"]))
    return body


IDENTITY = {"x-user-id": "u1", "x-tenant-id": "tenant-001"}


@pytest.mark.parametrize(
    ("code", "status", "type_id", "error_code"),
    [
        ("unauthorized", 401, "platform/unauthorized", "UNAUTHORIZED"),
        ("forbidden", 403, "platform/forbidden", "FORBIDDEN"),
        ("not_found", 404, "platform/not-found", "NOT_FOUND"),
        ("conflict", 409, "platform/conflict", "CONFLICT"),
        ("validation", 400, "platform/validation-failed", "VALIDATION_FAILED"),
        ("rule", 422, "platform/business-rule-violation", "BUSINESS_RULE_VIOLATION"),
        ("rate", 429, "platform/rate-limit-exceeded", "RATE_LIMIT_EXCEEDED"),
        ("unsupported", 501, "platform/not-implemented", "NOT_IMPLEMENTED"),
        ("notimpl", 501, "platform/not-implemented", "NOT_IMPLEMENTED"),
        ("unavailable", 503, "platform/service-unavailable", "SERVICE_UNAVAILABLE"),
        ("tender", 404, "tender/not-found", "TENDER_NOT_FOUND"),
        ("unhandled", 500, "platform/internal-error", "INTERNAL_ERROR"),
        ("http409", 409, "platform/conflict", "CONFLICT"),
        ("http500", 500, "platform/internal-error", "INTERNAL_ERROR"),
    ],
)
def test_status_mapping(code: str, status: int, type_id: str, error_code: str) -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    _problem(client.get(f"/boom/{code}", headers=IDENTITY), status, type_id, error_code)


def test_details_and_tenant() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    body = _problem(
        client.get("/boom/tender?token=abc", headers=IDENTITY),
        404,
        "tender/not-found",
        "TENDER_NOT_FOUND",
    )
    assert body["title"] == "Tender not found"
    assert body["detail"] == "Tender 123 was not found."
    assert body["instance"] == "/boom/tender"
    assert body["tenantId"] == "tenant-001"

    body = _problem(
        client.get("/boom/unauthorized", headers=IDENTITY),
        401,
        "platform/unauthorized",
        "UNAUTHORIZED",
    )
    assert "tenantId" not in body
    assert body["detail"] == "Missing or invalid authentication headers."

    resp = client.get("/boom/http409")
    assert _problem(resp, 409, "platform/conflict", "CONFLICT")["detail"] == "Version mismatch."
    assert resp.headers["retry-after"] == "1"


@pytest.mark.parametrize("code", ["unhandled", "secret500", "http500"])
def test_5xx_never_leaks(code: str) -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    body = _problem(client.get(f"/boom/{code}"), 500, "platform/internal-error", "INTERNAL_ERROR")
    assert body["title"] == "Internal Server Error"
    assert "detail" not in body
    body.pop("instance")
    for secret in ("hunter2", "alice@example.com", "pq:", "secret"):
        assert secret not in str(body)


def test_validation_errors_match_gincommon() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.post(
        "/tenders", json={"budget": 0, "stage": "closed", "items": [{"sku": "TOO-LONG-SKU"}]}
    )
    body = _problem(resp, 400, TYPE_VALIDATION_FAILED.id, "VALIDATION_FAILED")
    assert body["title"] == "Validation failed"
    assert body["validationErrors"] == [
        {"field": "title", "message": "is required"},
        {"field": "deadline", "message": "is required"},
        {"field": "budget", "message": "must be at least 1"},
        {"field": "stage", "message": "must be one of: draft, open"},
        {"field": "items[0].sku", "message": "must be at most 8 characters"},
    ]
    assert "TOO-LONG-SKU" not in resp.text and "closed" not in resp.text

    resp = client.post("/tenders", json={"title": 1, "deadline": "2027-01-01T00:00:00Z"})
    assert _problem(resp, 400, TYPE_VALIDATION_FAILED.id, "VALIDATION_FAILED")[
        "validationErrors"
    ] == [{"field": "title", "message": "must be a string"}]

    resp = client.post(
        "/tenders", content=b'{"title":', headers={"content-type": "application/json"}
    )
    body = _problem(resp, 400, TYPE_VALIDATION_FAILED.id, "VALIDATION_FAILED")
    assert (
        body["detail"] == "The request body is not valid JSON." and "validationErrors" not in body
    )

    resp = client.post("/tenders")
    assert (
        _problem(resp, 400, TYPE_VALIDATION_FAILED.id, "VALIDATION_FAILED")["detail"]
        == "The request body is empty."
    )

    body = _problem(
        client.get("/boom/validation"), 400, TYPE_VALIDATION_FAILED.id, "VALIDATION_FAILED"
    )
    assert body["validationErrors"] == [{"field": "deadline", "message": "must be in the future"}]


def test_route_errors() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    body = _problem(client.get("/does-not-exist"), 404, "platform/not-found", "ROUTE_NOT_FOUND")
    assert body["detail"] == "No route matches the request path."
    body = _problem(
        client.get("/only-put"), 405, "platform/method-not-allowed", "METHOD_NOT_ALLOWED"
    )
    assert body["detail"] == "The route does not support the request method."


def test_trace_id_resolution() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    resp = client.get("/boom/conflict", headers={"traceparent": tp, "x-request-id": "req-1"})
    assert resp.json()["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    resp = client.get("/boom/conflict", headers={"x-request-id": "req-abc123"})
    assert resp.json()["traceId"] == "req-abc123"
    assert resp.headers["X-Request-ID"] == "req-abc123"
    generated = client.get("/boom/conflict").json()["traceId"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)


def test_type_base_and_no_context_middleware() -> None:
    client = TestClient(
        _build_app(context=False, type_base="https://errors.example.com/"),
        raise_server_exceptions=False,
    )
    body = client.get("/boom/conflict").json()
    assert body["type"] == "https://errors.example.com/platform/conflict"
    assert "tenantId" not in body


def test_problem_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    from fastapicommon.logging import configure_logging

    configure_logging("INFO")
    client = TestClient(_build_app(service="orders-api"), raise_server_exceptions=False)
    with caplog.at_level("WARNING", logger="fastapicommon.problem"):
        client.get("/boom/tender", headers=IDENTITY)
    record = next(
        r
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["event"] == "problem response"
    )
    assert record.levelname == "WARNING"
    entry = record.msg
    assert entry["errorCode"] == "TENDER_NOT_FOUND"
    assert entry["service"] == "orders-api"
    assert entry["tenantId"] == "tenant-001"
    assert entry["status"] == 404
    line = str(entry)
    assert "Tender 123" not in line


def test_exception_classes() -> None:
    assert BusinessException is PlatformException
    assert ResourceNotFoundException is NotFoundException
    exc = PlatformException()
    assert exc.status_code == 500 and exc.code == "INTERNAL_ERROR" and exc.message is None
    exc = ConflictException("Locked.", code="TENDER_LOCKED")
    assert exc.status_code == 409 and exc.code == "TENDER_LOCKED" and str(exc) == "Locked."
    assert str(ConflictException()) == "Conflict"
    assert ValidationException().status_code == 400
