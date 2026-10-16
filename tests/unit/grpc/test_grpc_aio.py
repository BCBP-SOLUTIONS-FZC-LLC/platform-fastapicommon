"""AsyncContextServerInterceptor against a real grpc.aio server: spans, request context,
metrics, the grpc_request log, and exception sanitizing (no message reaches clients or logs)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from concurrent import futures
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


async def _write_stream(request: bytes, context: grpc.aio.ServicerContext) -> None:
    await context.write(b"w1")
    await context.write(b"w2")


def _sync_whoami(request: bytes, context: grpc.ServicerContext) -> bytes:
    return get_request_context().request_id.encode()


def _sync_stream(request: bytes, context: grpc.ServicerContext) -> Iterator[bytes]:
    yield b"a"
    yield b"b"


def _sync_aborted(request: bytes, context: grpc.ServicerContext) -> bytes:
    context.abort(grpc.StatusCode.PERMISSION_DENIED, "No.")
    return b""  # pragma: no cover


async def _slow(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
    await asyncio.sleep(5)
    return b""  # pragma: no cover


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
        "WriteStream": grpc.unary_stream_rpc_method_handler(_write_stream, _ident, _ident),
        "SyncWhoami": grpc.unary_unary_rpc_method_handler(_sync_whoami, _ident, _ident),
        "SyncStream": grpc.unary_stream_rpc_method_handler(_sync_stream, _ident, _ident),
        "SyncAborted": grpc.unary_unary_rpc_method_handler(_sync_aborted, _ident, _ident),
        "Slow": grpc.unary_unary_rpc_method_handler(_slow, _ident, _ident),
    }
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=2),
        interceptors=[AsyncContextServerInterceptor()],
    )
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


def _grpc_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Errors grpc.aio itself logged (e.g. "Unexpected [UsageError] raised by servicer")."""
    return [r for r in caplog.records if r.name.startswith("grpc") and r.levelno >= logging.ERROR]


async def test_handler_abort_is_not_aborted_again(
    channel: tuple[grpc.aio.Channel, CollectorRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: re-aborting after the handler's own abort raised UsageError('Abort already
    called!'), logged by grpc at ERROR with a traceback on every deliberate 4xx."""
    ch, registry = channel
    with caplog.at_level(logging.INFO), pytest.raises(grpc.aio.AioRpcError) as err:
        await ch.unary_unary("/test.Aio/Aborted")(b"")
    assert err.value.code() == grpc.StatusCode.NOT_FOUND
    assert _grpc_errors(caplog) == []
    assert (
        _value(
            registry,
            "platform_grpc_server_handled_total",
            grpc_method="/test.Aio/Aborted",
            grpc_code="NotFound",
            status_class="4xx",
            error_class="client_error",
        )
        == 1.0
    )


async def test_context_write_streams_are_kept(
    channel: tuple[grpc.aio.Channel, CollectorRegistry],
) -> None:
    ch, _ = channel
    assert [m async for m in ch.unary_stream("/test.Aio/WriteStream")(b"")] == [b"w1", b"w2"]


async def test_sync_handlers_still_run_in_the_thread_pool(
    channel: tuple[grpc.aio.Channel, CollectorRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: sync handlers were wrapped in async functions, so a sync stream sent no
    messages and a sync abort was an un-awaited coroutine."""
    ch, registry = channel
    with caplog.at_level(logging.INFO):
        assert await ch.unary_unary("/test.Aio/SyncWhoami")(b"", metadata=IDENTITY) == b"req-aio"
        assert [m async for m in ch.unary_stream("/test.Aio/SyncStream")(b"")] == [b"a", b"b"]
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await ch.unary_unary("/test.Aio/SyncAborted")(b"")
    assert err.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert err.value.details() == "No."
    assert _grpc_errors(caplog) == []
    assert (
        _value(
            registry,
            "platform_grpc_server_handled_total",
            grpc_method="/test.Aio/SyncWhoami",
            grpc_code="OK",
            status_class="2xx",
            error_class="success",
        )
        == 1.0
    )


async def _until(predicate: Any, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    ("cancel", "code", "status_class", "error_class"),
    [
        (False, "DeadlineExceeded", "4xx", "client_error"),
        (True, "Canceled", "4xx", "client_error"),
    ],
)
async def test_deadline_and_cancel_are_not_ok(
    channel: tuple[grpc.aio.Channel, CollectorRegistry],
    caplog: pytest.LogCaptureFixture,
    cancel: bool,
    code: str,
    status_class: str,
    error_class: str,
) -> None:
    """Regression: the handler task's CancelledError skipped the error path, so a timed-out or
    cancelled RPC was logged and counted as OK."""
    ch, registry = channel

    def counted() -> float:
        return _value(
            registry,
            "platform_grpc_server_handled_total",
            grpc_method="/test.Aio/Slow",
            grpc_code=code,
            status_class=status_class,
            error_class=error_class,
        )

    with caplog.at_level(logging.INFO):
        if cancel:
            call = ch.unary_unary("/test.Aio/Slow")(b"")
            await asyncio.sleep(0.2)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await ch.unary_unary("/test.Aio/Slow")(b"", timeout=0.2)
            assert err.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        await _until(lambda: counted() == 1.0)
    assert counted() == 1.0
    (entry,) = (
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict)
        and r.msg["event"] == "grpc_request"
        and r.msg["method"] == "/test.Aio/Slow"
    )
    assert entry["grpc_code"] == code
