"""OpenTelemetry tracing with platform-gincommon's contract (``internal/adapter/outbound/otel``,
``pkg/gincommon/tracing.go``), so Go and Python services export identical traces.

Configuration (environment, as in Go; :class:`TracingConfig` fields override it):

==========================================  ==================================================
``OTEL_EXPORTER_OTLP_ENDPOINT``             OTLP/gRPC ``host:port`` (default ``localhost:4317``;
                                            ``none`` / ``off`` / ``disabled``, or
                                            ``OTEL_TRACES_EXPORTER=none``, keep spans
                                            in-process)
``OTEL_EXPORTER_OTLP_INSECURE``             plaintext export (default: only in ``dev`` /
                                            ``local``)
``OTEL_TRACES_SAMPLER_RATIO`` (then         root sampling ratio (default 1.0 in dev/local, 0.1
``OTEL_TRACES_SAMPLER_ARG``)                otherwise), always parent-based
``OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED``  re-sample a sampled remote parent with the ratio
``OTEL_PROPAGATOR_BAGGAGE``                 propagate W3C baggage too (default false)
``OTEL_SERVICE_NAME`` (then ``APP_NAME``)    ``service.name`` for :func:`init_tracing_from_env`
==========================================  ==================================================

The resource carries ``service.name``, ``service.version``, ``service.namespace`` (the
observability domain) and ``deployment.environment.name``. Every install logs
``otel_tracing_initialized`` with the effective configuration; SDK errors are logged as
``otel_error``, a failed exporter as ``otel_exporter_init_failed``, and a failed flush as
``otel_shutdown_flush_failed``.

The library sets the OpenTelemetry global provider at most once, to a delegating provider:
installing again (a second app, a configured service name replacing a provisional
``unknown_service`` install) swaps the SDK provider behind it and flushes the old one, and
tracers obtained earlier follow the swap. A ``TracerProvider`` the service installed itself is
never replaced (Go TRC-1 … TRC-4).
"""

from __future__ import annotations

import math
import os
import sys
import threading
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace import NonRecordingSpan, Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from fastapicommon.logging import get_logger
from fastapicommon.logging.config import is_dev_env

if TYPE_CHECKING:
    from fastapi import FastAPI

UNKNOWN_SERVICE_NAME = "unknown_service"
DEFAULT_ENDPOINT = "localhost:4317"
SHUTDOWN_TIMEOUT_MILLIS = 5000

_logger = get_logger("fastapicommon.otel")


def _env_bool(key: str, fallback: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    if raw:
        print(
            f"fastapicommon: invalid {key}={raw!r}; using {str(fallback).lower()}", file=sys.stderr
        )
    return fallback


_DISABLED_ENDPOINTS = ("none", "off", "disabled")


def trace_export_disabled(endpoint: str | None = None) -> bool:
    """Whether span export is off: ``OTEL_TRACES_EXPORTER=none``, or the endpoint (``endpoint``
    when given, else ``OTEL_EXPORTER_OTLP_ENDPOINT``) is ``none`` / ``off`` / ``disabled``."""
    if os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower() == "none":
        return True
    if endpoint is None:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    return endpoint.strip().lower() in _DISABLED_ENDPOINTS


def _sample_ratio(dev: bool) -> float:
    default = 1.0 if dev else 0.1
    key = "OTEL_TRACES_SAMPLER_RATIO"
    raw = os.environ.get(key, "")
    if not raw:
        key, raw = "OTEL_TRACES_SAMPLER_ARG", os.environ.get("OTEL_TRACES_SAMPLER_ARG", "")
    if not raw:
        return default
    try:
        ratio = float(raw)
    except ValueError:
        ratio = math.nan
    if math.isnan(ratio):  # "nan" parses, and passes min/max clamping
        print(f"fastapicommon: invalid {key}={raw!r}; using {default}", file=sys.stderr)
        return default
    return min(max(ratio, 0.0), 1.0)


@dataclass(frozen=True)
class TracingConfig:
    """Tracing identity and export settings (Go ``TracingConfig`` + ``TelemetryConfig``)."""

    service_name: str = ""
    service_version: str = "unknown"
    domain: str = ""
    environment: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    insecure: bool = False
    sample_ratio: float = 0.1
    enable_baggage: bool = False
    ignore_remote_parent_sampled: bool = False
    provisional: bool = False

    @classmethod
    def from_env(
        cls,
        service_name: str = "",
        service_version: str = "",
        domain: str = "",
        environment: str = "",
        endpoint: str | None = None,
    ) -> TracingConfig:
        """Resolve every unset field from the environment (Go ``otel.EnvConfig``). An empty
        ``service_name`` gives a provisional ``unknown_service`` install that the first
        configured one replaces. ``environment`` wins over ``APP_ENV`` / ``ENVIRONMENT`` for the
        label and the dev defaults alike. An ``endpoint`` (``OTLP_ENDPOINT``) overrides
        ``OTEL_EXPORTER_OTLP_ENDPOINT``; either set to ``none`` / ``off`` / ``disabled``, or
        ``OTEL_TRACES_EXPORTER=none``, disables export."""
        environment = environment or os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT", "")
        dev = is_dev_env(environment)
        if trace_export_disabled(endpoint or None):
            endpoint = ""
        elif not endpoint:
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or DEFAULT_ENDPOINT
        provisional = not service_name.strip()
        return cls(
            service_name=service_name.strip() or UNKNOWN_SERVICE_NAME,
            service_version=service_version or os.environ.get("BUILD_VERSION") or "unknown",
            domain=(domain or os.environ.get("OBSERVABILITY_DOMAIN", "")).strip().lower(),
            environment=environment.strip().lower(),
            endpoint=endpoint,
            insecure=_env_bool("OTEL_EXPORTER_OTLP_INSECURE", dev),
            sample_ratio=_sample_ratio(dev),
            enable_baggage=_env_bool("OTEL_PROPAGATOR_BAGGAGE", False),
            ignore_remote_parent_sampled=_env_bool(
                "OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED", False
            ),
            provisional=provisional,
        )


def build_resource(config: TracingConfig) -> Resource:
    attributes: dict[str, str] = {
        "service.name": config.service_name or UNKNOWN_SERVICE_NAME,
        "service.version": config.service_version,
    }
    if config.domain:
        attributes["service.namespace"] = config.domain
    if config.environment:
        attributes["deployment.environment.name"] = config.environment
    return Resource.create(attributes)


def build_sampler(ratio: float, ignore_remote_parent_sampled: bool) -> Sampler:
    """Go ``Sampler``: parent-based, with a root decision of always (ratio ≥ 1), never (≤ 0) or
    trace-id ratio. With ``ignore_remote_parent_sampled`` a sampled remote parent is re-sampled
    with the root sampler, so external callers cannot force sampling."""
    if ratio >= 1.0:
        root: Sampler = ALWAYS_ON
    elif ratio <= 0:
        root = ALWAYS_OFF
    else:
        root = TraceIdRatioBased(ratio)
    if ignore_remote_parent_sampled:
        return ParentBased(root, remote_parent_sampled=root)
    return ParentBased(root)


# --------------------------------------------------------------------------------------------
# The delegating global provider
# --------------------------------------------------------------------------------------------


class _DelegatingTracer(Tracer):
    def __init__(
        self, provider: _DelegatingProvider, args: tuple[Any, ...], kwargs: dict[str, Any]
    ):
        self._provider, self._args, self._kwargs = provider, args, kwargs
        self._cache: tuple[TracerProvider | None, Tracer] | None = None

    def _current(self) -> Tracer | None:
        inner = self._provider.inner
        if inner is None:
            return None
        if self._cache is None or self._cache[0] is not inner:
            self._cache = (inner, inner.get_tracer(*self._args, **self._kwargs))
        return self._cache[1]

    def start_span(self, name: str, context: Any = None, *args: Any, **kwargs: Any) -> Span:
        tracer = self._current()
        if tracer is None:
            # No provider: keep the parent span context (Go's noop tracer does the same).
            return NonRecordingSpan(trace.get_current_span(context).get_span_context())
        return tracer.start_span(name, context, *args, **kwargs)

    @contextmanager
    def start_as_current_span(  # type: ignore[override]
        self, name: str, context: Any = None, *args: Any, **kwargs: Any
    ) -> Iterator[Span]:
        # Resolved on entry, not when called: as a decorator (applied once, often at import,
        # before tracing is installed) each call follows the provider active at that time.
        tracer = self._current()
        if tracer is None:
            with trace.use_span(
                self.start_span(name, context), end_on_exit=kwargs.get("end_on_exit", True)
            ) as span:
                yield span
            return
        with tracer.start_as_current_span(name, context, *args, **kwargs) as span:
            yield span


class _DelegatingProvider(trace.TracerProvider):
    def __init__(self) -> None:
        self.inner: TracerProvider | None = None

    def get_tracer(self, *args: Any, **kwargs: Any) -> Tracer:
        return _DelegatingTracer(self, args, kwargs)


@dataclass
class _Generation:
    provider: TracerProvider
    config: TracingConfig


_global = _DelegatingProvider()
_lock = threading.Lock()
_active: _Generation | None = None


def _is_global(provider: object) -> bool:
    return provider is _global


def _install(config: TracingConfig, exporter: SpanExporter | None) -> _Generation | None:
    """Build and swap in a provider; returns the replaced generation (flushed by the caller
    after the lock is released)."""
    global _active
    provider = TracerProvider(
        resource=build_resource(config),
        sampler=build_sampler(config.sample_ratio, config.ignore_remote_parent_sampled),
    )
    exporter_state = "disabled (no endpoint)"
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
        exporter_state = "custom"
    elif config.endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            otlp = OTLPSpanExporter(endpoint=config.endpoint, insecure=config.insecure)
        except Exception as exc:  # pragma: no cover - the exporter constructor rarely fails
            exporter_state = "failed (in-process only)"
            _logger.warning(
                "otel_exporter_init_failed",
                error=f"{type(exc).__name__}: {exc}",
                endpoint=config.endpoint,
                fallback="in-process TracerProvider; spans are not exported",
            )
        else:
            provider.add_span_processor(BatchSpanProcessor(otlp))
            exporter_state = "otlp-grpc"

    propagators: list[Any] = [TraceContextTextMapPropagator()]
    if config.enable_baggage:
        propagators.append(W3CBaggagePropagator())
    propagate.set_global_textmap(CompositePropagator(propagators))

    old = _active
    _active = _Generation(provider, config)
    _global.inner = provider
    if not _is_global(trace.get_tracer_provider()):
        trace.set_tracer_provider(_global)
    _logger.info(
        "otel_tracing_initialized",
        service=config.service_name,
        version=config.service_version,
        domain=config.domain,
        environment=config.environment,
        endpoint=config.endpoint,
        insecure=config.insecure,
        exporter=exporter_state,
        sample_ratio=config.sample_ratio,
        ignore_remote_parent_sampled=config.ignore_remote_parent_sampled,
        baggage=config.enable_baggage,
        provisional=config.provisional,
    )
    return old


def _flush(generation: _Generation | None) -> None:
    if generation is None:
        return
    provider = generation.provider
    try:
        if not provider.force_flush(SHUTDOWN_TIMEOUT_MILLIS):
            _logger.warning("otel_shutdown_flush_failed", error="force_flush timed out")
        provider.shutdown()
    except Exception as exc:  # pragma: no cover - SDK shutdown errors are rare
        _logger.warning("otel_shutdown_flush_failed", error=f"{type(exc).__name__}: {exc}")


def init_tracing(config: TracingConfig, exporter: SpanExporter | None = None) -> TracerProvider:
    """Install a tracer provider for ``config`` (replacing and flushing one this library
    installed earlier) and return it (Go ``InitTracingWithConfig``). ``exporter`` replaces the
    OTLP exporter (tests: an ``InMemorySpanExporter``)."""
    with _lock:
        old = _install(config, exporter)
        assert _active is not None
        provider = _active.provider
    _flush(old)
    return provider


def init_tracing_from_env() -> TracerProvider:
    """:func:`init_tracing` with ``OTEL_SERVICE_NAME`` (then ``APP_NAME``) and the ``OTEL_*``
    environment (Go ``InitTracingFromEnv``)."""
    name = os.environ.get("OTEL_SERVICE_NAME") or os.environ.get("APP_NAME", "")
    if not name.strip():
        print(
            "fastapicommon: WARNING tracing initialised without a service name (set "
            "OTEL_SERVICE_NAME or APP_NAME); spans use service.name=unknown_service until an app "
            "with a configured name installs tracing",
            file=sys.stderr,
        )
    return init_tracing(TracingConfig.from_env(service_name=name))


def ensure_tracing(
    config: TracingConfig, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Install tracing unless a provider is already in place (Go ``EnsureInitTelemetry``): one
    this library installed is kept unless it is provisional and ``config`` is not; a provider
    the service installed itself is never replaced (``None`` is returned then)."""
    with _lock:
        current = trace.get_tracer_provider()
        if _is_global(current) and _active is not None:
            if not (_active.config.provisional and not config.provisional):
                return _active.provider
        elif isinstance(current, TracerProvider):
            return None
        old = _install(config, exporter)
        assert _active is not None
        provider = _active.provider
    _flush(old)
    return provider


def flush_tracing() -> None:
    """Export the active provider's buffered spans, leaving it running (an app's shutdown: other
    apps and gRPC servers in the process share the provider). A provider the service installed
    itself is not touched."""
    with _lock:
        generation = _active if _is_global(trace.get_tracer_provider()) else None
    if generation is None:
        return
    try:
        if not generation.provider.force_flush(SHUTDOWN_TIMEOUT_MILLIS):
            _logger.warning("otel_shutdown_flush_failed", error="force_flush timed out")
    except Exception as exc:  # pragma: no cover - SDK flush errors are rare
        _logger.warning("otel_shutdown_flush_failed", error=f"{type(exc).__name__}: {exc}")


def shutdown_tracing() -> None:
    """Flush and stop the active provider (Go ``gincommon.Shutdown``'s tracing half), for every
    user in the process: call it once, at process shutdown (an app's own shutdown only
    :func:`flush_tracing`). The global provider stays installed and records nothing until the
    next install."""
    global _active
    with _lock:
        old, _active = _active, None
        _global.inner = None
    _flush(old)


def active_tracing_config() -> TracingConfig | None:
    return _active.config if _active is not None else None


# --------------------------------------------------------------------------------------------
# Tracing API for code outside the middleware (Go pkg/gincommon/tracing.go)
# --------------------------------------------------------------------------------------------


def new_tracer(scope: str) -> Tracer:
    """A tracer for ``scope`` from the installed provider; before tracing is installed it records
    nothing, and it follows later provider replacements (Go ``NewTracer``)."""
    return trace.get_tracer(scope)


def noop_tracer() -> Tracer:
    """A tracer that records nothing, for tests and tools (Go ``NoopTracer``)."""
    return trace.NoOpTracer()


def span_trace_id(context: otel_context.Context | None = None) -> str:
    """The trace id of the current (or ``context``'s) span, ``""`` without a valid span (Go
    ``SpanTraceID``)."""
    span_context = trace.get_current_span(context).get_span_context()
    return trace.format_trace_id(span_context.trace_id) if span_context.is_valid else ""


def record_span_error(
    error: BaseException, description: str, context: otel_context.Context | None = None
) -> None:
    """Record ``error`` on the current span and mark it failed with ``description`` (Go
    ``RecordSpanError``); a no-op without a recording span."""
    span = trace.get_current_span(context)
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, description))


def inject_trace_context(
    headers: MutableMapping[str, str], context: otel_context.Context | None = None
) -> MutableMapping[str, str]:
    """Write the W3C ``traceparent`` (and baggage, when enabled) for the current span into
    ``headers`` (Go ``InjectTraceContext``); identity and request-id headers are not set."""
    propagate.inject(headers, context=context)
    return headers


def extract_trace_context(carrier: Mapping[str, str]) -> otel_context.Context:
    return propagate.extract(carrier)


# --------------------------------------------------------------------------------------------
# App / gRPC wiring
# --------------------------------------------------------------------------------------------


def setup_tracing(
    app: FastAPI,
    service_name: str,
    exporter: SpanExporter | None = None,
    config: TracingConfig | None = None,
) -> TracerProvider:
    """Install tracing for ``service_name`` (the ``OTEL_*`` environment fills the rest, or pass
    ``config``) and add the HTTP server-span middleware to ``app``. ``create_platform_app`` does
    this itself; call it only for an app built without it. ``exporter`` replaces the OTLP
    exporter."""
    from fastapicommon.middleware.tracing import TracingMiddleware

    resolved = config or TracingConfig.from_env(service_name=service_name)
    provider = init_tracing(resolved, exporter)
    app.add_middleware(TracingMiddleware)
    return provider


def instrument_grpc_server() -> None:
    """Instrument every gRPC server of the process with OpenTelemetry's generic interceptor.
    Not needed with ``ContextServerInterceptor``, which starts gincommon-compatible server spans
    itself (both together would nest two spans per RPC). Requires the ``grpc`` extra."""
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer

    GrpcInstrumentorServer().instrument()  # type: ignore[no-untyped-call]


def instrument_grpc_client() -> None:
    """Instrument the process's gRPC client channels for tracing (``traceparent`` on outbound
    calls). Requires the ``grpc`` extra."""
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    GrpcInstrumentorClient().instrument()  # type: ignore[no-untyped-call]
