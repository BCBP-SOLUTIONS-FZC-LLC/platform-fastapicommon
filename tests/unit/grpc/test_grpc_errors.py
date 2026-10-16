"""The gRPC error sanitizer against a real grpcio server: no exception message ever reaches
the client (grpcio's "Exception calling application: <message>") or the logs."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent import futures
from typing import Any

import grpc
import pytest

from fastapicommon.grpc.server import MSG_INTERNAL, ContextServerInterceptor
from fastapicommon.logging import configure_logging

SECRET = "pq: duplicate key users_email_key email=john@example.com"
IDENTITY = (("x-request-id", "req-1"), ("x-user-id", "u1"), ("x-tenant-id", "t1"))


class RepoError(Exception):
    pass


def _ident(b: bytes) -> bytes:
    return b


def _plain(request: bytes, context: grpc.ServicerContext) -> bytes:
    try:
        raise ValueError(SECRET)
    except ValueError as exc:
        raise RepoError(f"create: {exc}") from exc


def _aborted(request: bytes, context: grpc.ServicerContext) -> bytes:
    context.abort(grpc.StatusCode.NOT_FOUND, "Tender 7 was not found.")
    return b""  # pragma: no cover


def _set_code_then_raise(request: bytes, context: grpc.ServicerContext) -> bytes:
    context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
    raise RuntimeError(SECRET)


def _ok(request: bytes, context: grpc.ServicerContext) -> bytes:
    return b"pong"


def _server_stream(request: bytes, context: grpc.ServicerContext) -> Iterator[bytes]:
    yield b"one"
    raise RuntimeError(SECRET)


def _client_stream(requests: Iterator[bytes], context: grpc.ServicerContext) -> bytes:
    for _ in requests:
        pass
    raise RuntimeError(SECRET)


def _bidi(requests: Iterator[bytes], context: grpc.ServicerContext) -> Iterator[bytes]:
    yield from requests
    raise RuntimeError(SECRET)


@pytest.fixture
def channel() -> Iterator[grpc.Channel]:
    configure_logging("INFO")
    handlers = {
        "Plain": grpc.unary_unary_rpc_method_handler(_plain, _ident, _ident),
        "Aborted": grpc.unary_unary_rpc_method_handler(_aborted, _ident, _ident),
        "SetCode": grpc.unary_unary_rpc_method_handler(_set_code_then_raise, _ident, _ident),
        "Ok": grpc.unary_unary_rpc_method_handler(_ok, _ident, _ident),
        "ServerStream": grpc.unary_stream_rpc_method_handler(_server_stream, _ident, _ident),
        "ClientStream": grpc.stream_unary_rpc_method_handler(_client_stream, _ident, _ident),
        "Bidi": grpc.stream_stream_rpc_method_handler(_bidi, _ident, _ident),
    }
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4), interceptors=[ContextServerInterceptor()]
    )
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("test.Errors", handlers),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield ch
    finally:
        ch.close()
        server.stop(None)


def _entries(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        dict(r.msg)
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "unclassified error"
    ]


def _no_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    for r in caplog.records:
        text = r.getMessage() + str(r.exc_info or "") + str(r.exc_text or "")
        assert "john@example.com" not in text and "pq:" not in text, r.getMessage()


def _unary(ch: grpc.Channel, name: str) -> Any:
    return ch.unary_unary(f"/test.Errors/{name}")(b"x", metadata=IDENTITY, timeout=5)


def test_unclassified_exception_is_internal_with_fixed_message(
    channel: grpc.Channel, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(grpc.RpcError) as info:
        _unary(channel, "Plain")
    assert info.value.code() == grpc.StatusCode.INTERNAL
    assert info.value.details() == MSG_INTERNAL
    (f,) = _entries(caplog)
    assert f["classified"] is False and f["level"] == "error"
    assert f["grpc_method"] == "/test.Errors/Plain" and f["grpc_code"] == "Internal"
    assert f["request_id"] == "req-1"
    assert f["error_chain"] == [f"{__name__}.RepoError", "ValueError"]
    assert any(" in _plain" in fr for fr in f["stack"])
    _no_secret_logged(caplog)  # grpcio's own "Exception calling application" log is gone too


def test_explicit_status_is_kept(channel: grpc.Channel, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(grpc.RpcError) as info:
            _unary(channel, "Aborted")
        assert info.value.code() == grpc.StatusCode.NOT_FOUND
        assert info.value.details() == "Tender 7 was not found."

        # set_code + raise: grpcio would have used "Exception calling application: <msg>"
        # as the details.
        with pytest.raises(grpc.RpcError) as info:
            _unary(channel, "SetCode")
        assert info.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert SECRET not in (info.value.details() or "")
    assert _entries(caplog) == []
    _no_secret_logged(caplog)
    assert _unary(channel, "Ok") == b"pong"


@pytest.mark.parametrize("kind", ["ServerStream", "ClientStream", "Bidi"])
def test_streaming_exceptions_sanitized(
    kind: str, channel: grpc.Channel, caplog: pytest.LogCaptureFixture
) -> None:
    method = f"/test.Errors/{kind}"
    with caplog.at_level(logging.DEBUG), pytest.raises(grpc.RpcError) as info:
        if kind == "ServerStream":
            list(channel.unary_stream(method)(b"x", timeout=5))
        elif kind == "ClientStream":
            channel.stream_unary(method)(iter([b"a", b"b"]), timeout=5)
        else:
            list(channel.stream_stream(method)(iter([b"a"]), timeout=5))
    assert info.value.code() == grpc.StatusCode.INTERNAL
    assert info.value.details() == MSG_INTERNAL
    (f,) = _entries(caplog)
    assert f["grpc_method"] == method and f["error_type"] == "RuntimeError"
    _no_secret_logged(caplog)


class _NoAbortContext:
    """A context without abort() (e.g. a test double): the original exception is re-raised."""

    def __init__(self, code: Any = None) -> None:
        self._code = code

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()

    def code(self) -> Any:
        return self._code

    def set_code(self, code: Any) -> None:
        self._code = code


def _wrap(handler: grpc.RpcMethodHandler, service: str = "") -> grpc.RpcMethodHandler:
    interceptor = ContextServerInterceptor()
    interceptor._service = service
    return interceptor.intercept_service(lambda _: handler, type("D", (), {"method": "/t.S/M"})())


def test_without_abort_the_exception_is_reraised(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")
    with caplog.at_level(logging.DEBUG):
        # Explicit status, no abort: re-raised, not flagged.
        h = _wrap(grpc.unary_unary_rpc_method_handler(_set_code_then_raise))
        with pytest.raises(RuntimeError):
            h.unary_unary(b"x", _NoAbortContext(grpc.StatusCode.NOT_FOUND))
        assert _entries(caplog) == []
        # Streaming, unclassified, no abort: logged (with the service), then re-raised.
        h = _wrap(grpc.unary_stream_rpc_method_handler(_server_stream), service="orders-api")
        with pytest.raises(RuntimeError):
            list(h.unary_stream(b"x", _NoAbortContext()))
        h = _wrap(grpc.stream_unary_rpc_method_handler(_client_stream))
        with pytest.raises(RuntimeError):
            h.stream_unary(iter([b"a"]), _NoAbortContext())
    entries = _entries(caplog)
    assert len(entries) == 2 and entries[0]["service"] == "orders-api"
    _no_secret_logged(caplog)
