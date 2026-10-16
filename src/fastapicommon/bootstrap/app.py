from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import FastAPI

from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import UnhandledErrorMiddleware, register_exception_handlers
from fastapicommon.health import health_router
from fastapicommon.logging import configure_logging
from fastapicommon.metrics import PrometheusMiddleware, init_metrics_from_settings, metrics_router
from fastapicommon.middleware import ContextMiddleware, TimeoutMiddleware
from fastapicommon.middleware.logging import LoggingMiddleware
from fastapicommon.middleware.tracing import TracingMiddleware
from fastapicommon.telemetry import TracingConfig, ensure_tracing, shutdown_tracing


def create_platform_app(
    app_name: str | None = None,
    settings: PlatformSettings | None = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a FastAPI app with platform logging, middleware, metrics, tracing, health
    probes, and exception handlers already wired up — platform-gincommon's
    ``ObservabilityMiddlewares`` stack, in the same order: tracing (server span), request
    context, the ``http_request`` access log, metrics, timeout.

    The consuming service should only add its own routers, repositories, use cases, and
    domain logic on top of the returned app — this library owns no business logic.

    Metrics are the ``platform_*`` metrics of the Enterprise Platform Observability Standard,
    and they need a domain: ``settings.observability_domain`` or ``OBSERVABILITY_DOMAIN``.
    Raises ``ValueError`` (where Go's ``ObservabilityMiddlewares`` panics) on a missing or unknown
    domain, an ``app_name`` that is not a valid service label, or a missing or out-of-vocabulary
    environment. With ``enable_metrics=False`` no domain is needed (a configured one is still
    validated). Metrics initialization is process-wide: the first app created in the process
    sets the identity.

    Tracing follows gincommon: the ``OTEL_*`` environment configures export and sampling
    (``settings.otlp_endpoint`` overrides ``OTEL_EXPORTER_OTLP_ENDPOINT``), the first configured
    install in the process is kept, and the app's shutdown flushes buffered spans (a
    ``lifespan`` passed in ``fastapi_kwargs`` still runs, inside it).
    """
    settings = settings or PlatformSettings()
    if app_name:
        settings = settings.model_copy(update={"app_name": app_name})

    init_metrics_from_settings(settings)

    environment = settings.environment if "environment" in settings.model_fields_set else ""
    configure_logging(settings.log_level or None, environment or None)

    if settings.enable_tracing:
        ensure_tracing(_tracing_config(settings, environment))
        fastapi_kwargs["lifespan"] = _flush_tracing_on_shutdown(fastapi_kwargs.get("lifespan"))

    app = FastAPI(title=settings.app_name, **fastapi_kwargs)

    # Middleware added last runs outermost.
    app.add_middleware(
        TimeoutMiddleware,  # type: ignore[arg-type]
        timeout_seconds=settings.request_timeout_seconds,
    )
    if settings.enable_metrics:
        app.add_middleware(PrometheusMiddleware)
        app.include_router(metrics_router)
    # Inside LoggingMiddleware (which logs the 500), outside metrics (which record
    # the 500 first): answers unclassified exceptions without re-raising them to the
    # server, so their message is never logged.
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(ContextMiddleware)
    if settings.enable_tracing:
        app.add_middleware(TracingMiddleware)

    app.include_router(health_router)
    register_exception_handlers(
        app, type_base=settings.problem_type_base, service=settings.app_name
    )
    return app


def _tracing_config(settings: PlatformSettings, environment: str) -> TracingConfig:
    """Go ``telemetryConfig`` from the settings' identity: service name, build version, domain
    (``service.namespace``) and environment, the rest from ``OTEL_*``."""
    return TracingConfig.from_env(
        service_name=settings.app_name,
        service_version=settings.build_version,
        domain=settings.observability_domain,
        environment=environment,
        endpoint=settings.otlp_endpoint or None,
    )


_Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[Any]]


def _flush_tracing_on_shutdown(inner: _Lifespan | None) -> _Lifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
        try:
            if inner is None:
                yield None
            else:
                async with inner(app) as state:
                    yield state
        finally:
            shutdown_tracing()

    return lifespan
