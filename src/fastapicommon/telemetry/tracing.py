from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter


def setup_tracing(
    app: FastAPI,
    service_name: str,
    exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Configure OpenTelemetry tracing and instrument the given FastAPI app.

    Trace context is propagated via the standard ``traceparent`` header. If ``exporter``
    is omitted, spans are recorded but not exported — pass an OTLP exporter to ship
    spans to a collector.
    """
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return provider


def instrument_grpc_server() -> None:
    """Instrument the process's gRPC server for tracing. Requires the ``grpc`` extra."""
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer

    GrpcInstrumentorServer().instrument()  # type: ignore[no-untyped-call]


def instrument_grpc_client() -> None:
    """Instrument the process's gRPC client channels for tracing. Requires the ``grpc``
    extra."""
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    GrpcInstrumentorClient().instrument()  # type: ignore[no-untyped-call]
