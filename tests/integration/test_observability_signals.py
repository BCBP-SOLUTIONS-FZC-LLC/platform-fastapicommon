"""Logs and traces through a real ``create_platform_app()`` app and the gRPC interceptor, checked
against platform-gincommon's contract (LLD §7.2 traces, §7.3 log event catalogue)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastapicommon.auth import require_authenticated_user
from fastapicommon.bootstrap import FASTAPI_NATIVE_TELEMETRY_OFF, create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.grpc import ContextServerInterceptor
from fastapicommon.logging import configure_logging
from fastapicommon.rbac import RequireAllRoles, RequireAnyRole, RequireRole
from fastapicommon.telemetry import TracingConfig, init_tracing

pytestmark = pytest.mark.integration

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


class _Spans:
    def __init__(self, exporter: InMemorySpanExporter, provider: TracerProvider) -> None:
        self.exporter, self.provider = exporter, provider

    def finished(self) -> list[ReadableSpan]:
        self.provider.force_flush()
        return list(self.exporter.get_finished_spans())


@pytest.fixture
def spans() -> Iterator[_Spans]:
    exporter = InMemorySpanExporter()
    config = TracingConfig(
        service_name="orders-api", domain="iam", environment="prod", endpoint="", sample_ratio=1.0
    )
    yield _Spans(exporter, init_tracing(config, exporter))


def _app() -> FastAPI:
    settings = PlatformSettings(
        _env_file=None,
        app_name="orders-api",
        observability_domain="iam",
        environment="prod",
        build_version="v1",
    )
    app = create_platform_app(settings=settings)

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    @app.post("/limited")
    async def limited() -> Response:
        return Response(status_code=429)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret-in-message")

    @app.get("/me", dependencies=[Depends(require_authenticated_user)])
    async def me() -> dict[str, str]:
        return {}

    @app.delete("/items/{item_id}", dependencies=[Depends(RequireRole("admin"))])
    async def delete(item_id: str) -> dict[str, str]:
        return {}

    @app.get("/any", dependencies=[Depends(RequireAnyRole("a", "b"))])
    async def any_role() -> dict[str, str]:
        return {}

    @app.get("/all", dependencies=[Depends(RequireAllRoles("a", "b"))])
    async def all_roles() -> dict[str, str]:
        return {}

    return app


needs_native_telemetry = pytest.mark.skipif(
    not FASTAPI_NATIVE_TELEMETRY_OFF, reason="FastAPI before 0.143 has no native telemetry"
)


@needs_native_telemetry
def test_fastapi_native_telemetry_is_off(spans: _Spans) -> None:
    """Regression: FastAPI 0.143 instruments itself by default, so every request had a second
    root server span (``GET /items/{item_id}``, scope ``fastapi``) beside the platform span."""
    TestClient(_app()).get("/items/7")
    (span,) = spans.finished()
    assert span.name == "GET /items/:item_id"
    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "platform-fastapicommon/http"


@needs_native_telemetry
def test_a_services_own_telemetry_setting_is_kept(spans: _Spans) -> None:
    """``telemetry=`` passed to create_platform_app replaces the library's default."""
    settings = PlatformSettings(
        _env_file=None, app_name="orders-api", observability_domain="iam", environment="prod"
    )
    app = create_platform_app(settings=settings, telemetry={"tracing": True, "metrics": False})

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    TestClient(app).get("/items/7")
    scopes = sorted(
        s.instrumentation_scope.name for s in spans.finished() if s.instrumentation_scope
    )
    assert "fastapi" in scopes and "platform-fastapicommon/http" in scopes


def _entries(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line[:1] == "{"]


def _by_msg(entries: list[dict[str, Any]], msg: str) -> list[dict[str, Any]]:
    return [e for e in entries if e["msg"] == msg]


def _client(capsys: pytest.CaptureFixture[str], **kwargs: Any) -> TestClient:
    client = TestClient(_app(), **kwargs)
    configure_logging(environment="prod")
    capsys.readouterr()
    return client


def test_http_request_access_log(capsys: pytest.CaptureFixture[str], spans: _Spans) -> None:
    client = _client(capsys, raise_server_exceptions=False)
    identity = {"traceparent": TRACEPARENT, "x-user-id": "u1", "x-tenant-id": "t1"}
    client.get("/items/42", headers=identity)
    client.get("/nope", headers={"x-user-id": "bad\x7fid" + "x" * 300})
    client.get("/boom")
    ok, missing, boom = _by_msg(_entries(capsys), "http_request")

    assert ok["level"] == "info"
    assert {k: ok[k] for k in ("trace_id", "method", "path", "status", "tenant_id", "user_id")} == {
        "trace_id": TRACE_ID,
        "method": "GET",
        "path": "/items/:item_id",
        "status": 200,
        "tenant_id": "t1",
        "user_id": "u1",
    }
    assert isinstance(ok["duration_ms"], float) and ok["request_id"]
    assert missing["level"] == "warn" and missing["path"] == "unknown" and missing["status"] == 404
    assert "user_id" not in missing and "\x7f" not in missing["claimed_user_id"]
    assert len(missing["claimed_user_id"].encode()) <= 256
    assert boom["level"] == "error" and boom["status"] == 500 and "stacktrace" in boom
    assert "secret-in-message" not in json.dumps(boom)


def test_audit_entries(capsys: pytest.CaptureFixture[str], spans: _Spans) -> None:
    client = _client(capsys, raise_server_exceptions=False)
    rejected_headers = {"x-tenant-id": "t1", "x-forwarded-for": "10.0.0.1"}
    assert client.get("/me", headers=rejected_headers).status_code == 401
    denied_headers = {"x-user-id": "u1", "x-tenant-roles": "viewer"}
    assert client.delete("/items/1", headers=denied_headers).status_code == 403
    client.get("/any", headers={"x-user-id": "u1"})
    client.get("/all", headers={"x-user-id": "u1", "x-tenant-roles": "a"})
    entries = _entries(capsys)

    (rejected,) = _by_msg(entries, "auth: request rejected")
    assert rejected["level"] == "warn"
    assert {k: rejected[k] for k in ("reason", "method", "route", "status", "client_ip")} == {
        "reason": "missing or invalid x-user-id",
        "method": "GET",
        "route": "/me",
        "status": 401,
        "client_ip": "testclient",  # the peer: no trusted proxies, X-Forwarded-For ignored
    }
    assert rejected["claimed_tenant_id"] == "t1" and rejected["request_id"]
    denied = _by_msg(entries, "authz: access denied")
    assert [d["action"] for d in denied] == [
        "require_role:admin",
        "require_any_role:a,b",
        "require_all_roles:a,b",
    ]
    assert denied[0]["resource"] == "/items/:item_id" and denied[0]["roles"] == ["viewer"]
    assert denied[0]["user_id"] == "u1" and denied[0]["tenant_id"] == ""


def test_rate_limited_log_is_bounded(capsys: pytest.CaptureFixture[str], spans: _Spans) -> None:
    client = _client(capsys)
    for _ in range(3):
        client.post("/limited")
    (entry,) = _by_msg(_entries(capsys), "rate limited")
    assert {k: entry[k] for k in ("route", "method", "status", "suppressed")} == {
        "route": "/limited",
        "method": "POST",
        "status": 429,
        "suppressed": 0,
    }


def test_http_server_spans(spans: _Spans) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    client.get("/items/42?q=1", headers={"traceparent": TRACEPARENT, "user-agent": "ua"})
    client.request("PURGE", "/nope")
    client.get("/boom")
    item, other, boom = spans.finished()

    assert item.name == "GET /items/:item_id" and item.kind.name == "SERVER"
    assert format(item.context.trace_id, "032x") == TRACE_ID and item.parent is not None
    attrs = dict(item.attributes or {})
    assert attrs["http.route"] == "/items/:item_id" and attrs["url.path"] == "/items/42"
    assert attrs["http.response.status_code"] == 200 and attrs["user_agent.original"] == "ua"
    assert item.status.status_code.name == "UNSET"
    resource = dict(item.resource.attributes)
    assert resource["service.name"] == "orders-api" and resource["service.namespace"] == "iam"
    assert resource["deployment.environment.name"] == "prod"

    other_attrs = dict(other.attributes or {})
    assert other.name == "HTTP unknown" and other_attrs["http.request.method"] == "_OTHER"
    assert other_attrs["http.request.method_original"] == "PURGE"

    assert boom.status.status_code.name == "ERROR" and boom.status.description == "panic"
    assert dict(boom.attributes or {})["http.response.status_code"] == 500
    assert [dict(e.attributes or {}) for e in boom.events] == [{"exception.type": "RuntimeError"}]


class _Context:
    def __init__(
        self, code: grpc.StatusCode | None = None, metadata: tuple[tuple[str, str], ...] = ()
    ) -> None:
        self._code, self._metadata = code, metadata

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    def code(self) -> grpc.StatusCode | None:
        return self._code


def _call(method: str, context: _Context, behavior: Any = None) -> None:
    handler = grpc.unary_unary_rpc_method_handler(behavior or (lambda request, ctx: b"ok"))
    wrapped = ContextServerInterceptor().intercept_service(
        lambda _: handler, SimpleNamespace(method=method)
    )
    wrapped.unary_unary(b"req", context)


def test_grpc_spans_and_logs(capsys: pytest.CaptureFixture[str], spans: _Spans) -> None:
    configure_logging(environment="prod")
    capsys.readouterr()
    metadata = (("traceparent", TRACEPARENT), ("x-user-id", "u1"), ("x-tenant-id", "t1"))
    _call("/iam.v1.Events/Get", _Context(None, metadata))
    _call("/iam.v1.Events/Get", _Context(grpc.StatusCode.NOT_FOUND))
    _call("bad method", _Context(grpc.StatusCode.INTERNAL))

    def explode(request: Any, context: Any) -> Any:
        raise RuntimeError("secret")

    with pytest.raises(RuntimeError):
        _call("/iam.v1.Events/Get", _Context(), explode)

    ok, not_found, bad, panic = spans.finished()
    ok_attrs = dict(ok.attributes or {})
    assert ok.name == "/iam.v1.Events/Get" and format(ok.context.trace_id, "032x") == TRACE_ID
    assert (ok_attrs["rpc.service"], ok_attrs["rpc.method"]) == ("iam.v1.Events", "Get")
    assert (ok_attrs["rpc.response.status_code"], ok_attrs["rpc.grpc.status_code"]) == ("OK", 0)
    assert not_found.status.status_code.name == "ERROR"
    assert dict(not_found.attributes or {})["rpc.response.status_code"] == "NOT_FOUND"
    bad_attrs = dict(bad.attributes or {})
    assert bad.name == "unknown" and bad_attrs["rpc.method"] == "_OTHER"
    assert bad_attrs["rpc.method_original"] == "bad method"
    assert panic.status.description == "panic"
    assert [dict(e.attributes or {}) for e in panic.events] == [{"exception.type": "RuntimeError"}]

    logs = _by_msg(_entries(capsys), "grpc_request")
    assert [(e["level"], e["grpc_code"], e["method"]) for e in logs] == [
        ("info", "OK", "/iam.v1.Events/Get"),
        ("warn", "NotFound", "/iam.v1.Events/Get"),
        ("error", "Internal", "unknown"),
        ("error", "Unknown", "/iam.v1.Events/Get"),
    ]
    assert logs[0]["user_id"] == "u1" and logs[0]["tenant_id"] == "t1"
    assert isinstance(logs[0]["duration_ms"], float) and logs[0]["request_id"]
    assert "user_id" not in logs[1]


def test_metrics_handler_error_is_logged_once(
    capsys: pytest.CaptureFixture[str], spans: _Spans, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapicommon.metrics import router

    def broken(registry: object) -> bytes:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(router, "generate_latest", broken)
    monkeypatch.setattr(router, "_last_logged", {})
    client = _client(capsys)
    assert client.get("/metrics").status_code == 500
    assert client.get("/metrics").status_code == 500
    (entry,) = _by_msg(_entries(capsys), "metrics handler error")
    assert entry["error"] == "RuntimeError: collector exploded"
    monkeypatch.setattr(router, "_MAX_TRACKED_ERRORS", 1)
    router._log_metrics_error("another")  # evicts the oldest tracked error
    assert list(router._last_logged) == ["another"]


def test_failure_after_response_started_marks_the_span_aborted(spans: _Spans) -> None:
    """gincommon spanAttrAborted: a response cut short is annotated, not an Error span."""
    app = _app()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body():  # type: ignore[no-untyped-def]
            yield b"partial"
            raise RuntimeError("secret-in-message")

        return StreamingResponse(body())

    TestClient(app).get("/stream")
    (span,) = spans.finished()
    assert span.name == "GET /stream"
    assert dict(span.attributes or {})["http.aborted"] is True
    assert [e.name for e in span.events] == ["http.aborted"]
    assert span.status.status_code.name == "UNSET"


async def test_cancelled_request_is_aborted_not_a_panic(
    spans: _Spans, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: a request the server cancels (graceful shutdown, client disconnect) was a
    500 panic in the span, the access log and the metrics, so every rollout fired panic and
    error-rate alerts. Go records such an abort as 499, not a failure."""
    import asyncio

    from prometheus_client import REGISTRY

    app = _app()
    entered = asyncio.Event()

    @app.get("/hang")
    async def hang() -> None:
        entered.set()
        await asyncio.sleep(30)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/hang",
        "raw_path": b"/hang",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"t")],
        "client": ("127.0.0.1", 1),
        "server": ("t", 80),
        "app": app,
    }

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(60)
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:  # pragma: no cover - nothing is sent
        pass

    async def call() -> None:
        await app(scope, receive, send)  # type: ignore[arg-type]

    with caplog.at_level("DEBUG"):
        task = asyncio.ensure_future(call())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    (span,) = (s for s in spans.finished() if s.name == "GET /hang")
    attrs = dict(span.attributes or {})
    assert attrs["http.response.status_code"] == 499 and attrs["http.aborted"] is True
    assert span.status.status_code.name == "UNSET"
    (entry,) = (
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "http_request"
    )
    assert entry["status"] == 499 and entry["aborted"] is True and entry["level"] == "debug"
    labels = {"domain": "iam", "environment": "prod", "service": "orders-api"}
    assert not REGISTRY.get_sample_value(
        "platform_http_panics_total", {**labels, "method": "GET", "route": "/hang"}
    )
    assert (
        REGISTRY.get_sample_value(
            "platform_http_requests_total",
            {
                **labels,
                "method": "GET",
                "route": "/hang",
                "status_class": "4xx",
                "error_class": "client_error",
            },
        )
        == 1.0
    )
