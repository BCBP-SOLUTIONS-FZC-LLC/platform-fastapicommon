"""TracingMiddleware on its own (no ContextMiddleware in front): the client address it records,
and the span status for every class of response."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fastapicommon.middleware import ContextMiddleware
from fastapicommon.middleware.tracing import TracingMiddleware
from fastapicommon.telemetry import TracingConfig, init_tracing, shutdown_tracing

PEER = ("198.51.100.4", 50000)
SPOOFED = {"x-forwarded-for": "6.6.6.6", "x-real-ip": "7.7.7.7"}


class _Spans:
    def __init__(self, exporter: InMemorySpanExporter, provider: TracerProvider) -> None:
        self.exporter, self.provider = exporter, provider

    def only(self) -> ReadableSpan:
        self.provider.force_flush()
        finished = self.exporter.get_finished_spans()
        assert len(finished) == 1, [s.name for s in finished]
        return finished[0]


@pytest.fixture
def spans() -> Iterator[_Spans]:
    exporter = InMemorySpanExporter()
    config = TracingConfig(service_name="svc", endpoint="", sample_ratio=1.0)
    yield _Spans(exporter, init_tracing(config, exporter))
    shutdown_tracing()


def _app(with_context: bool = False, trusted_proxies: str | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/status/{code}")
    async def status(code: int) -> Response:
        return Response(status_code=code)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret-in-message")

    if with_context:
        app.add_middleware(ContextMiddleware, trusted_proxies=trusted_proxies)
    app.add_middleware(TracingMiddleware)
    return app


def test_client_address_without_context_middleware_is_the_peer(spans: _Spans) -> None:
    """Forwarding headers are never believed without the trusted-proxy configuration, even when
    TracingMiddleware runs without ContextMiddleware (which normally resolves the client IP)."""
    TestClient(_app(), client=PEER).get("/status/200", headers=SPOOFED)
    assert spans.only().attributes["client.address"] == "198.51.100.4"


def test_client_address_uses_the_trusted_proxy_resolution(spans: _Spans) -> None:
    client = TestClient(_app(with_context=True, trusted_proxies="198.51.100.0/24"), client=PEER)
    client.get("/status/200", headers={"x-forwarded-for": "203.0.113.9"})
    assert spans.only().attributes["client.address"] == "203.0.113.9"


@pytest.mark.parametrize(
    ("code", "status", "description"),
    [
        (200, StatusCode.UNSET, None),
        (404, StatusCode.UNSET, None),  # 4xx is the client's error, not the server span's
        (503, StatusCode.ERROR, "Service Unavailable"),
        (599, StatusCode.ERROR, None),  # no standard phrase: Error status without description
    ],
)
def test_span_status_by_response_class(
    spans: _Spans, code: int, status: StatusCode, description: str | None
) -> None:
    TestClient(_app()).get(f"/status/{code}")
    span = spans.only()
    assert span.name == "GET /status/:code"
    assert span.attributes["http.response.status_code"] == code
    assert span.status.status_code == status
    assert (span.status.description or None) == description


def test_escaped_exception_marks_the_span_panic_without_its_message(spans: _Spans) -> None:
    """Without UnhandledErrorMiddleware (an app built by hand), an exception escapes to
    TracingMiddleware: Go's recovered-panic span, Error status ``panic``, the exception type
    recorded and never its message."""
    with pytest.raises(RuntimeError):
        TestClient(_app()).get("/boom")
    span = spans.only()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "panic"
    assert span.attributes["http.response.status_code"] == 500
    (event,) = (e for e in span.events if e.name == "exception")
    assert event.attributes["exception.type"] == "RuntimeError"
    assert "secret-in-message" not in str(dict(event.attributes)) + str(span.attributes)
