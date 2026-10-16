from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from fastapicommon.exceptions import (
    ConflictException,
    PlatformException,
    UnhandledErrorMiddleware,
    register_exception_handlers,
)
from fastapicommon.exceptions.handlers import error_chain_types, stack_frames
from fastapicommon.logging import configure_logging
from fastapicommon.middleware import ContextMiddleware
from fastapicommon.problem import PROBLEM_MEDIA_TYPE

SECRET = "pq: duplicate key users_email_key email=john@example.com"


class DbError(Exception):
    pass


class RepoError(Exception):
    pass


def create_tender(tender_id: str) -> None:
    try:
        raise DbError(SECRET)
    except DbError as exc:
        raise RepoError(f"create tender {tender_id}: {exc}") from exc


def _app(*, middleware: bool = True) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app, service="orders-api")
    if middleware:
        app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(ContextMiddleware)

    @app.post("/tenders/{tender_id}")
    async def create(tender_id: str) -> dict[str, str]:
        create_tender(tender_id)
        return {}

    @app.get("/group")
    async def group() -> dict[str, str]:
        raise ExceptionGroup("batch", [ValueError(SECRET), KeyError("k")])

    @app.get("/classified")
    async def classified() -> dict[str, str]:
        raise ConflictException("Taken.")

    @app.get("/internal")
    async def internal() -> dict[str, str]:
        raise PlatformException()  # deliberate 500

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"partial"
            raise RuntimeError(SECRET)

        return StreamingResponse(body())

    return app


def _entries(caplog: pytest.LogCaptureFixture) -> list[tuple[logging.LogRecord, dict[str, object]]]:
    out = []
    for r in caplog.records:
        if isinstance(r.msg, dict) and r.msg.get("event") == "unclassified error":
            out.append((r, dict(r.msg)))
    return out


@pytest.fixture(autouse=True)
def _logging() -> None:
    configure_logging("INFO")


def test_unclassified_error_logged_as_types_and_frames_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # raise_server_exceptions=True (the default): the exception must NOT reach the server,
    # which would log its message ("Exception in ASGI application").
    client = TestClient(_app())
    with caplog.at_level("INFO"):
        resp = client.post("/tenders/7?token=abc", headers={"x-request-id": "req-abc123"})

    assert resp.status_code == 500
    assert resp.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = resp.json()
    assert body["type"].endswith("/platform/internal-error") and "detail" not in body
    assert "pq:" not in resp.text

    entries = _entries(caplog)
    assert len(entries) == 1
    record, f = entries[0]
    assert record.levelname == "ERROR"
    assert f["classified"] is False
    assert f["request_id"] == "req-abc123" and f["traceId"] == "req-abc123"
    assert f["service"] == "orders-api"
    assert f["method"] == "POST" and f["route"] == "/tenders/:tender_id"
    assert f["status"] == 500 and f["errorCode"] == "INTERNAL_ERROR"
    assert f["error_type"] == f"{__name__}.RepoError"
    assert f["error_chain"] == [f"{__name__}.RepoError", f"{__name__}.DbError"]
    assert f["operation"] == f"{__name__}._app.<locals>.create"
    stack = f["stack"]
    assert isinstance(stack, list) and any(" in create_tender" in fr for fr in stack)

    # No log record anywhere carries the exception text.
    for r in caplog.records:
        text = r.getMessage() + str(r.exc_info or "")
        assert (
            "john@example.com" not in text and "pq:" not in text and "create tender 7" not in text
        )


def test_exception_group_chain(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        TestClient(_app()).get("/group")
    ((_, f),) = _entries(caplog)
    assert f["error_chain"] == ["ExceptionGroup", "ValueError", "KeyError"]


@pytest.mark.parametrize("path", ["/classified", "/internal"])
def test_classified_errors_not_flagged(path: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        TestClient(_app()).get(path)
    assert _entries(caplog) == []


def test_response_already_started_is_logged_not_reraised(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        try:
            TestClient(_app()).get("/stream")
        except Exception as exc:  # pragma: no cover - the failure being asserted
            pytest.fail(f"exception reached the server: {exc!r}")
    ((_, f),) = _entries(caplog)
    # Regression: the traceId was hard-coded "" here, so the entry could not be joined.
    assert f["error_type"] == "RuntimeError" and f["traceId"]
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_handler_alone_still_logs_types(caplog: pytest.LogCaptureFixture) -> None:
    # Without the middleware Starlette re-raises after the handler (documented); the
    # handler's own entry is still types-only.
    with caplog.at_level("INFO"):
        resp = TestClient(_app(middleware=False), raise_server_exceptions=False).post("/tenders/1")
    assert resp.status_code == 500
    ((_, f),) = _entries(caplog)
    assert f["error_type"] == f"{__name__}.RepoError"


def test_chain_and_frames_helpers() -> None:
    a = ValueError("a")
    b = KeyError("b")
    b.__context__ = a
    b.__suppress_context__ = True
    assert error_chain_types(b) == ["KeyError"]  # `raise ... from None` hides the context
    loop = ValueError("loop")
    loop.__context__ = loop  # a cycle must terminate
    assert error_chain_types(loop) == ["ValueError"]
    deep = ExceptionGroup("g", [ValueError(str(i)) for i in range(20)])
    assert len(error_chain_types(deep)) == 8
    assert stack_frames(ValueError("never raised")) == []


async def test_non_http_scope_passes_through() -> None:
    seen: list[str] = []

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    await UnhandledErrorMiddleware(inner)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_exception_before_routing_has_no_operation(caplog: pytest.LogCaptureFixture) -> None:
    # A middleware inside UnhandledErrorMiddleware fails before any endpoint is matched.
    from starlette.middleware.base import BaseHTTPMiddleware

    class Exploding(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
            raise RuntimeError(SECRET)

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(Exploding)
    app.add_middleware(UnhandledErrorMiddleware)
    with caplog.at_level("INFO"):
        resp = TestClient(app).get("/anything")
    assert resp.status_code == 500
    ((_, f),) = _entries(caplog)
    assert f["operation"] == "" and f["error_type"] == "RuntimeError"
