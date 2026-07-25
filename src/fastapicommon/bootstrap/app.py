from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import register_exception_handlers
from fastapicommon.health import health_router
from fastapicommon.logging import configure_logging
from fastapicommon.metrics import PrometheusMiddleware, metrics_router
from fastapicommon.middleware import ContextMiddleware, TimeoutMiddleware
from fastapicommon.telemetry import setup_tracing


def create_platform_app(
    app_name: str | None = None,
    settings: PlatformSettings | None = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a FastAPI app with platform logging, middleware, metrics, tracing, health
    probes, and exception handlers already wired up.

    The consuming service should only add its own routers, repositories, use cases, and
    domain logic on top of the returned app — this library owns no business logic.
    """
    settings = settings or PlatformSettings()
    if app_name:
        settings = settings.model_copy(update={"app_name": app_name})

    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, **fastapi_kwargs)

    # Middleware added last runs outermost, so context is bound before metrics/timeout
    # run and remains available to the endpoint itself.
    app.add_middleware(
        TimeoutMiddleware,  # type: ignore[arg-type]
        timeout_seconds=settings.request_timeout_seconds,
    )
    if settings.enable_metrics:
        app.add_middleware(PrometheusMiddleware)
        app.include_router(metrics_router)
    app.add_middleware(ContextMiddleware)

    app.include_router(health_router)
    register_exception_handlers(app)

    if settings.enable_tracing:
        setup_tracing(app, service_name=settings.app_name, exporter=_build_span_exporter(settings))

    return app


def _build_span_exporter(settings: PlatformSettings) -> SpanExporter | None:
    """Build an OTLP/gRPC span exporter from ``settings.otlp_endpoint``, or None to
    record spans without exporting them (the default when no collector is configured).
    """
    if not settings.otlp_endpoint:
        return None

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    insecure = settings.environment in ("dev", "development", "local")
    return OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=insecure)
