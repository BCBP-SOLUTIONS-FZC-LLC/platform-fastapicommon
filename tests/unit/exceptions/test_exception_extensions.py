"""Extension members through the exception handlers — the port of gincommon v2.1.0's
``test/unit/gincommon/extensions_test.go`` and the extension case of
``test/unit/middleware/problem_test.go``."""

from __future__ import annotations

import logging
import math
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fastapicommon.exceptions import (
    BusinessRuleViolationException,
    ConflictException,
    PlatformException,
    problem_response,
    register_exception_handlers,
)
from fastapicommon.exceptions import handlers as handlers_module
from fastapicommon.logging import configure_logging
from fastapicommon.middleware import ContextMiddleware
from fastapicommon.problem import PROBLEM_MEDIA_TYPE, TYPE_CONFLICT, parse_problem

IDENTITY = {"x-user-id": "u1", "x-tenant-id": "tenant-001"}


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app, service="problem-test")
    app.add_middleware(ContextMiddleware)

    @app.put("/api/tenants/{tenant_id}")
    async def update(tenant_id: str) -> None:
        raise (
            ConflictException("The tenant was modified concurrently.", code="VERSION_CONFLICT")
            .with_extension("record_version", 7)
            .with_extension("tenantId", "other-tenant")  # a standard member: dropped
            .with_extension("traceId", "forged")
        )

    @app.delete("/api/users/{user_id}")
    async def remove(user_id: str) -> None:
        raise BusinessRuleViolationException(
            "The user is a delegate on active workflows."
        ).with_extensions({"active_workflows": ["wf-1", "wf-2"], "licensed_seats": 10})

    @app.get("/api/internal")
    async def internal() -> None:
        raise PlatformException("pq: password=hunter2").with_extension(
            "query_text", "SELECT secret FROM vault"
        )

    @app.get("/api/nan")
    async def nan() -> None:
        raise ConflictException("Conflict.").with_extension("score", math.nan)

    @app.get("/api/unserialisable")
    async def unserialisable() -> None:
        raise ConflictException("Conflict.", extensions={"seat_ids": {"s1"}})

    @app.get("/api/direct")
    async def direct(request: Request) -> JSONResponse:
        return problem_response(
            request, TYPE_CONFLICT, detail="Direct.", extensions={"record_version": 3}
        )

    return app


def _client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _entries(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if isinstance(r.msg, dict) and r.msg["event"] == event]


def test_exception_extension_api() -> None:
    e = ConflictException("The tenant was modified concurrently.")
    assert e.extensions == {}
    same = e.with_extension("record_version", 6).with_extensions(
        {"record_version": 7, "licensed_seats": 10}
    )
    assert same is e
    assert e.extensions == {"record_version": 7, "licensed_seats": 10}
    assert "record_version" not in str(e)
    given = {"record_version": 1}
    e = ConflictException(extensions=given).with_extension("record_version", 2)
    assert given == {"record_version": 1}  # the constructor copies


def test_extensions_follow_the_standard_members(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")
    with caplog.at_level("WARNING", logger="fastapicommon.problem"):
        resp = _client().put("/api/tenants/t1", headers=IDENTITY)
    assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM_MEDIA_TYPE
    p = parse_problem(resp.json())
    assert (p.error_code, p.instance, p.tenant_id) == (
        "VERSION_CONFLICT",
        "/api/tenants/t1",
        "tenant-001",
    )
    assert p.trace_id not in ("forged", "", None)
    assert p.extensions == {"record_version": 7}
    assert p.extension("record_version", int) == 7
    body = resp.text
    assert body.endswith(',"record_version":7}')
    assert body.count('"tenantId"') == 1 and body.count('"traceId"') == 1

    # Logging is unchanged: one warn entry with the usual fields and no extensions.
    (entry,) = _entries(caplog, "problem response")
    assert entry.levelname == "WARNING"
    assert "record_version" not in entry.msg  # type: ignore[operator]
    assert entry.msg["tenantId"] == "tenant-001"  # type: ignore[index]
    assert entry.msg["errorCode"] == "VERSION_CONFLICT"  # type: ignore[index]
    assert _entries(caplog, "problem extensions dropped") == []


def test_with_extensions() -> None:
    resp = _client().delete("/api/users/u2", headers=IDENTITY)
    assert resp.status_code == 422
    assert resp.text.endswith(',"active_workflows":["wf-1","wf-2"],"licensed_seats":10}')
    assert resp.json()["detail"] == "The user is a delegate on active workflows."


def test_problem_response_extensions() -> None:
    resp = _client().get("/api/direct")
    assert resp.status_code == 409 and resp.text.endswith(',"record_version":3}')


def test_internal_error_extensions_never_leak() -> None:
    resp = _client().get("/api/internal", headers=IDENTITY)
    assert resp.status_code == 500
    p = parse_problem(resp.json())
    assert p.extensions == {} and p.detail is None
    assert "SELECT" not in resp.text and "hunter2" not in resp.text


@pytest.mark.parametrize(
    ("path", "value_text"), [("/api/nan", "NaN"), ("/api/unserialisable", "s1")]
)
def test_unserialisable_extension_dropped(
    path: str, value_text: str, caplog: pytest.LogCaptureFixture
) -> None:
    """An extension value that cannot be serialised is dropped, the problem is still sent,
    and the drop is logged without the value."""
    configure_logging("INFO")
    with caplog.at_level("WARNING", logger="fastapicommon.problem"):
        resp = _client().get(path, headers=IDENTITY)
    assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM_MEDIA_TYPE
    p = parse_problem(resp.json())
    assert p.extensions == {} and p.detail == "Conflict."
    (dropped,) = _entries(caplog, "problem extensions dropped")
    fields: dict[str, Any] = dropped.msg  # type: ignore[assignment]
    assert dropped.levelname == "WARNING"
    assert fields["errorCode"] == "CONFLICT"
    assert fields["traceId"] == p.trace_id
    assert fields["service"] == "problem-test"
    assert fields["status"] == 409 and fields["type"] == p.type
    assert "score" not in fields and "seat_ids" not in fields and "tenantId" not in fields
    assert value_text not in str(fields)
    assert len(_entries(caplog, "problem response")) == 1


def test_failing_logger_does_not_affect_the_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("logger down")

    monkeypatch.setattr(handlers_module._problem_log, "warning", boom)
    for path in ("/api/nan", "/api/tenants/t1"):
        resp = _client().request("GET" if path == "/api/nan" else "PUT", path, headers=IDENTITY)
        assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM_MEDIA_TYPE
