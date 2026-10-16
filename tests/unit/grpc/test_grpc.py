from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import grpc

from fastapicommon.context import (
    CurrentUser,
    RequestContext,
    get_request_context_or_none,
    request_context_scope,
)
from fastapicommon.grpc.client import ContextClientInterceptor, _ClientCallDetails
from fastapicommon.grpc.server import ContextServerInterceptor, _context_from_metadata


def test_context_from_metadata_builds_request_context() -> None:
    metadata = (
        ("x-user-id", "u1"),
        ("x-tenant-id", "t1"),
        ("x-tenant-roles", "admin, operator"),
        ("x-request-id", "req-1"),
        ("x-correlation-id", "corr-1"),
    )
    context = _context_from_metadata(metadata)
    assert context.request_id == "req-1"
    assert context.correlation_id == "corr-1"
    assert context.user_id == "u1"
    assert context.tenant_id == "t1"
    assert context.roles == ("admin", "operator")


def test_context_from_metadata_generates_ids_when_absent() -> None:
    context = _context_from_metadata(())
    assert context.request_id
    assert context.correlation_id == context.request_id
    assert context.user_id is None
    assert context.roles == ()


class _FakeServicerContext:
    def __init__(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


def test_context_server_interceptor_binds_context_and_records_metrics() -> None:
    captured: dict[str, Any] = {}

    def behavior(request: Any, context: Any) -> Any:
        captured["user_id"] = get_request_context_or_none().user_id  # type: ignore[union-attr]
        return "response"

    handler = grpc.unary_unary_rpc_method_handler(behavior)

    def continuation(handler_call_details: Any) -> Any:
        return handler

    interceptor = ContextServerInterceptor()
    handler_call_details = SimpleNamespace(method="/svc/Method")
    wrapped_handler = interceptor.intercept_service(continuation, handler_call_details)

    fake_context = _FakeServicerContext((("x-user-id", "u1"),))
    result = wrapped_handler.unary_unary("request", fake_context)

    assert result == "response"
    assert captured["user_id"] == "u1"
    assert get_request_context_or_none() is None


def test_context_server_interceptor_records_error_status_and_reraises() -> None:
    def behavior(request: Any, context: Any) -> Any:
        raise ValueError("boom")

    handler = grpc.unary_unary_rpc_method_handler(behavior)

    def continuation(handler_call_details: Any) -> Any:
        return handler

    interceptor = ContextServerInterceptor()
    wrapped_handler = interceptor.intercept_service(
        continuation, SimpleNamespace(method="/svc/Failing")
    )

    try:
        wrapped_handler.unary_unary("request", _FakeServicerContext(()))
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_context_server_interceptor_passes_through_non_unary_handlers() -> None:
    interceptor = ContextServerInterceptor()

    def continuation(handler_call_details: Any) -> Any:
        return None

    result = interceptor.intercept_service(continuation, SimpleNamespace(method="/svc/Method"))
    assert result is None


def test_context_client_interceptor_appends_headers() -> None:
    context = RequestContext(
        request_id="r1", correlation_id="c1", user=CurrentUser(user_id="u1", tenant_id="t1")
    )

    captured: dict[str, Any] = {}

    def continuation(details: Any, request: Any) -> Any:
        captured["metadata"] = details.metadata
        return "response"

    details = _ClientCallDetails(
        method="/svc/Method",
        timeout=None,
        metadata=[],
        credentials=None,
        wait_for_ready=None,
        compression=None,
    )

    interceptor = ContextClientInterceptor()
    with request_context_scope(context):
        result = interceptor.intercept_unary_unary(continuation, details, "request")

    assert result == "response"
    assert ("x-user-id", "u1") in captured["metadata"]
    assert ("x-tenant-id", "t1") in captured["metadata"]
    assert ("x-request-id", "r1") in captured["metadata"]
    assert ("x-correlation-id", "c1") in captured["metadata"]


def test_context_client_interceptor_no_headers_when_unbound() -> None:
    captured: dict[str, Any] = {}

    def continuation(details: Any, request: Any) -> Any:
        captured["metadata"] = details.metadata
        return "response"

    details = _ClientCallDetails(
        method="/svc/Method",
        timeout=None,
        metadata=None,
        credentials=None,
        wait_for_ready=None,
        compression=None,
    )

    ContextClientInterceptor().intercept_unary_unary(continuation, details, "request")
    assert captured["metadata"] == []


def test_failing_metric_still_resets_context_and_ends_span(monkeypatch: Any) -> None:
    """Regression: finish() ran metrics, log, context reset and span end in sequence, so a
    failing step left the RequestContext / OTel context bound on the reused worker thread."""
    from opentelemetry import trace

    from fastapicommon.grpc import server as server_module

    class _Broken:
        def grpc_inflight_inc(self, method: str) -> None:
            pass

        def grpc_inflight_dec(self, method: str) -> None:
            self.decremented = True

        def record_grpc(self, *args: Any) -> None:
            raise RuntimeError("bad label")

    broken = _Broken()
    monkeypatch.setattr(server_module, "platform_metrics", lambda: broken)
    handler = grpc.unary_unary_rpc_method_handler(lambda request, context: "ok")
    wrapped = ContextServerInterceptor().intercept_service(
        lambda details: handler, SimpleNamespace(method="/svc/Method")
    )
    outer_span = trace.get_current_span()
    with contextlib.suppress(RuntimeError):
        wrapped.unary_unary("request", _FakeServicerContext((("x-user-id", "u1"),)))
    assert get_request_context_or_none() is None
    assert trace.get_current_span() is outer_span
    assert broken.decremented


def test_context_from_metadata_validates_ids() -> None:
    """Regression: gRPC used x-request-id / x-correlation-id verbatim (Go's request_id
    interceptor applies NormalizeRequestID); invalid ones are replaced like on HTTP."""
    context = _context_from_metadata(
        (("x-request-id", "bad id\n"), ("x-correlation-id", "<script>"))
    )
    assert context.request_id != "bad id\n" and len(context.request_id) == 36  # a new UUID
    assert context.correlation_id == context.request_id
    kept = _context_from_metadata((("x-request-id", "req-9"), ("x-correlation-id", "corr-9")))
    assert (kept.request_id, kept.correlation_id) == ("req-9", "corr-9")
