"""AsyncContextServerInterceptor against a real grpc.aio server: spans, request context,
metrics, the grpc_request log, and exception sanitizing (no message reaches clients or logs)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry

from fastapicommon.context import get_request_context
from fastapicommon.grpc import AsyncContextServerInterceptor
from fastapicommon.grpc.server import MSG_INTERNAL
from fastapicommon.logging import configure_logging
from fastapicommon.metrics import MetricsOptions, init_metrics
from fastapicommon.telemetry import TracingConfig, init_tracing

SECRET = "pq: duplicate key users_email_key email=john@example.com"
IDENTITY = (("x-request-id", "req-aio"), ("x-user-id", "u1"), ("x-tenant-id", "t1"))
CONST = {"domain": "iam", "environment": "prod", "service": "orders-api"}


def _ident(b: bytes) -> bytes:
    return b


async def _whoami(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
    ctx = get_request_context()
    assert trace.get_current_span().is_recording()
    return f"{ctx.request_id}/{ctx.user_id}".encode()


async def _plain(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
    raise RuntimeError(SECRET)


async def _aborted(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
    await context.abort(grpc.StatusCode.NOT_FOUND, "Tender 7 was not found.")
    return b""  # pragma: no cover


async def _server_stream(request: bytes, context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
    yield b"one"
    raise RuntimeError(SECRET)


async def _client_stream(requests: AsyncIterator[bytes], context: Any) -> bytes:
    async for _ in requests:
        pass
    raise RuntimeError(SECRET)


@pytest.fixture
async def channel() -> AsyncIterator[tuple[grpc.aio.Channel, CollectorRegistry]]:
    configure_logging("INFO")
    registry = CollectorRegistry()
    init_metrics(MetricsOptions("orders-api", domain="iam", environment="prod"), registry=registry)
    handlers = {
        "Whoami": grpc.unary_unary_rpc_method_handler(_whoami, _ident, _ident),
        "Plain": grpc.unary_unary_rpc_method_handler(_plain, _ident, _ident),
        "Aborted": grpc.unary_unary_rpc_method_handler(_aborted, _ident, _ident),
        "ServerStream": grpc.unary_stream_rpc_method_handler(_server_stream, _ident, _ident),
        "ClientStream": grpc.stream_unary_rpc_method_handler(_client_stream, _ident, _ident),
    }
    server = grpc.aio.server(interceptors=[AsyncContextServerInterceptor()])
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("test.Aio", handlers),))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield ch, registry
    finally:
        await ch.close()
        await server.stop(None)


def _value(registry: CollectorRegistry, name: str, **labels: str) -> float:
    return float(registry.get_sample_value(name, {**CONST, **labels}) or 0.0)


def _no_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    for r in caplog.records:
        assert SECRET not in str(r.msg) + str(r.exc_info or "") + str(r.exc_text or "")


async def test_context_span_metrics_and_log(
    channel: tuple[grpc.aio.Channel, CollectorRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    ch, registry = channel
    exporter = InMemorySpanExporter()
    provider = init_tracing(
        TracingConfig(service_name="orders-api", endpoint="", sample_ratio=1.0), exporter
    )
    with caplog.at_level(logging.INFO):
        reply = await ch.unary_unary("/test.Aio/Whoami")(b"", metadata=IDENTITY)
    assert reply == b"req-aio/u1"
    assert (
        _value(
            registry,
            "platform_grpc_server_handled_total",
            grpc_method="/test.Aio/Whoami",
            grpc_code="OK",
            status_class="2xx",
            error_class="success",
        )
        == 1.0
    )
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.name == "/test.Aio/Whoami" and span.kind.name == "SERVER"
    entry = next(
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["event"] == "grpc_request"
    )
    assert entry["grpc_code"] == "OK" and entry["request_id"] == "req-aio"


async def test_unclassified_exception_is_internal_without_message(
    channel: tuple[grpc.aio.Channel, CollectorRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    ch, registry = channel
    with caplog.at_level(logging.INFO), pytest.raises(grpc.aio.AioRpcError) as err:
        await ch.unary_unary("/test.Aio/Plain")(b"", metadata=IDENTITY)
    assert err.value.code() == grpc.StatusCode.INTERNAL
    assert err.value.details() == MSG_INTERNAL
    (entry,) = (
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["event"] == "unclassified error"
    )
    assert entry["grpc_code"] == "Internal" and entry["error_type"] == "RuntimeError"
    assert _value(registry, "platform_unclassified_errors_total", transport="grpc") == 1.0
    _no_secret_logged(caplog)


async def test_explicit_status_is_kept(
    channel: tuple[grpc.aio.Channel, CollectorRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    ch, registry = channel
    with caplog.at_level(logging.INFO), pytest.raises(grpc.aio.AioRpcError) as err:
        await ch.unary_unary("/test.Aio/Aborted")(b"")
    assert err.value.code() == grpc.StatusCode.NOT_FOUND
    assert err.value.details() == "Tender 7 was not found."
    assert not [
        r
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["event"] == "unclassified error"
    ]
    assert _value(registry, "platform_unclassified_errors_total", transport="grpc") == 0.0


async def test_streaming_exceptions_sanitized(
    channel: tuple[grpc.aio.Channel, CollectorRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    ch, _ = channel
    with caplog.at_level(logging.INFO):
        call = ch.unary_stream("/test.Aio/ServerStream")(b"")
        received = []
        with pytest.raises(grpc.aio.AioRpcError) as err:
            async for item in call:
                received.append(item)
        assert received == [b"one"]
        assert err.value.code() == grpc.StatusCode.INTERNAL
        assert err.value.details() == MSG_INTERNAL

        async def requests() -> AsyncIterator[bytes]:
            yield b"a"

        with pytest.raises(grpc.aio.AioRpcError) as err2:
            await ch.stream_unary("/test.Aio/ClientStream")(requests())
        assert err2.value.code() == grpc.StatusCode.INTERNAL
    _no_secret_logged(caplog)
