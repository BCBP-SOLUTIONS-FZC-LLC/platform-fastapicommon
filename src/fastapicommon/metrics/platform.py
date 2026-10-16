"""Enterprise Platform Observability Standard — ``platform_*`` metrics.

Python port of ``platform-gincommon``'s ``internal/adapter/outbound/metrics/platform.go`` (Go is
the source of truth). :func:`init_metrics` registers the ``platform_*`` collectors, built from
their :mod:`~fastapicommon.metrics.registry` entries, with the registry's const labels
(``domain``, ``service``, ``environment``) on every series. These are the only HTTP/gRPC metrics
the library emits; the domain (``PlatformSettings.observability_domain`` /
``OBSERVABILITY_DOMAIN``) is mandatory whenever metrics are enabled.

Initialization is process-wide and, like Go's ``sync.Once``-guarded ``Init``, the first
:func:`init_metrics` call wins; passing an explicit ``registry`` (tests) bypasses the guard and
replaces the active collectors, like Go's ``InitWithRegistererOptions``. Identity validation runs
on every call, so a misconfigured identity always raises ``ValueError`` at startup (where Go
panics).
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.metrics import MetricWrapperBase
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector
from starlette.routing import Match, Mount

from fastapicommon.metrics.registry import MetricSpec, ObservabilityRegistry, default_registry

if TYPE_CHECKING:
    from fastapicommon.config import PlatformSettings

#: Label value for requests with no registered route template / RPCs with no method name.
UNKNOWN = "unknown"

#: Mirrors Go's ``defaultVersion``: an empty build version is reported as ``unknown``.
DEFAULT_VERSION = "unknown"

_STANDARD_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)

#: grpc.StatusCode member name -> Go ``codes.Code.String()`` (the registry's grpc_code values).
_GRPC_CODE_NAMES: Mapping[str, str] = {
    "OK": "OK",
    "CANCELLED": "Canceled",
    "UNKNOWN": "Unknown",
    "INVALID_ARGUMENT": "InvalidArgument",
    "DEADLINE_EXCEEDED": "DeadlineExceeded",
    "NOT_FOUND": "NotFound",
    "ALREADY_EXISTS": "AlreadyExists",
    "PERMISSION_DENIED": "PermissionDenied",
    "RESOURCE_EXHAUSTED": "ResourceExhausted",
    "FAILED_PRECONDITION": "FailedPrecondition",
    "ABORTED": "Aborted",
    "OUT_OF_RANGE": "OutOfRange",
    "UNIMPLEMENTED": "Unimplemented",
    "INTERNAL": "Internal",
    "UNAVAILABLE": "Unavailable",
    "DATA_LOSS": "DataLoss",
    "UNAUTHENTICATED": "Unauthenticated",
}

#: Go ``GRPCStatusClass`` / ``GRPCRequestOutcome``: these codes are client-side (4xx).
_GRPC_CLIENT_CODES = frozenset(
    {
        "Canceled",
        "InvalidArgument",
        "NotFound",
        "AlreadyExists",
        "PermissionDenied",
        "Unauthenticated",
        "FailedPrecondition",
        "OutOfRange",
        "Aborted",
        "DeadlineExceeded",
        "ResourceExhausted",
    }
)

try:  # FastAPI ≥ 0.12x: included routers are lazy; see _flatten_routes.
    from fastapi.routing import iter_route_contexts as _iter_route_contexts
except ImportError:  # pragma: no cover - older FastAPI (exercised by the lowest-deps job)
    _iter_route_contexts = None  # type: ignore[assignment]

#: FastAPI/Starlette path parameter, e.g. ``{item_id}``, ``{item_id:int}``, ``{rest:path}``.
_PATH_PARAM = re.compile(r"\{([^}:]+)(?::([^}]*))?\}")


# --------------------------------------------------------------------------------------------
# Label helpers (mirror Go's HTTPMethodLabel / HTTPStatusClass / RequestOutcome / gRPC variants)
# --------------------------------------------------------------------------------------------


def http_method_label(method: str) -> str:
    """Return ``method`` if it is a standard HTTP method, else ``"OTHER"``."""
    return method if method in _STANDARD_HTTP_METHODS else "OTHER"


def http_status_class(status_code: int) -> str:
    """2xx for 200-399, 4xx for 400-499, else 5xx — Go ``HTTPStatusClass`` exactly."""
    if 200 <= status_code < 400:
        return "2xx"
    if 400 <= status_code < 500:
        return "4xx"
    return "5xx"


def http_error_class(status_code: int) -> str:
    """success / client_error / server_error — Go ``RequestOutcome`` exactly."""
    if 200 <= status_code < 400:
        return "success"
    if 400 <= status_code < 500:
        return "client_error"
    return "server_error"


def grpc_code_label(status_code_name: str) -> str:
    """Map a ``grpc.StatusCode`` member name (``"NOT_FOUND"``) to Go's code string
    (``"NotFound"``), the registry's ``grpc_code`` vocabulary."""
    return _GRPC_CODE_NAMES.get(status_code_name, "Unknown")


def grpc_status_class(code: str) -> str:
    if code == "OK":
        return "2xx"
    return "4xx" if code in _GRPC_CLIENT_CODES else "5xx"


def grpc_error_class(code: str) -> str:
    if code == "OK":
        return "success"
    return "client_error" if code in _GRPC_CLIENT_CODES else "server_error"


#: Go ``label.GRPCMethod``: ``/package.Service/Method``, at most 256 bytes.
_GRPC_METHOD = re.compile(r"/[A-Za-z0-9_.]+/[A-Za-z0-9_]+")
MAX_GRPC_METHOD_LEN = 256


def grpc_method_label(full_method: str | None) -> str:
    """``full_method`` when it is a well-formed ``/package.Service/Method`` of at most 256
    bytes, else ``"unknown"`` (Go ``label.GRPCMethod``), so client-chosen method names cannot
    explode label or span-name cardinality."""
    if (
        not full_method
        or len(full_method.encode()) > MAX_GRPC_METHOD_LEN
        or not _GRPC_METHOD.fullmatch(full_method)
    ):
        return UNKNOWN
    return full_method


def route_template_label(path: str) -> str:
    """Render a Starlette route template in the registry's ``route`` syntax (Gin-style), so Go
    and Python services report the same route identically: ``/items/{item_id}`` ->
    ``/items/:item_id``, ``/files/{rest:path}`` -> ``/files/*rest``."""

    def _replace(match: re.Match[str]) -> str:
        prefix = "*" if match.group(2) == "path" else ":"
        return prefix + match.group(1)

    return _PATH_PARAM.sub(_replace, path)


def _flatten_routes(routes: Sequence[Any]) -> Iterable[Any]:
    """FastAPI ≥ 0.12x keeps included routers as lazy entries without a ``path``; its public
    ``iter_route_contexts`` (what FastAPI's OpenAPI generation uses) yields each effective
    route with its full, prefixed template. Older versions copy the routes into the app's
    router, so they are already flat."""
    if _iter_route_contexts is None:
        return routes
    # Lazily: matching stops at the first full match instead of wrapping every route first.
    return _iter_route_contexts(routes)


def _match_routes(
    routes: Sequence[Any], scope: MutableMapping[str, Any], prefix: str
) -> str | None:
    for route in _flatten_routes(routes):
        match, child_scope = route.matches(scope)
        if match is not Match.FULL:
            continue
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            return None
        full_path = prefix + path
        original = getattr(route, "original_route", route)
        if isinstance(original, Mount):
            if original.routes:
                return _match_routes(original.routes, {**scope, **child_scope}, full_path)
            # An opaque mounted ASGI app (e.g. StaticFiles): its mount prefix is the template.
            return full_path or None
        return full_path
    return None


def http_route_label(scope: MutableMapping[str, Any]) -> str:
    """The route template a request will be dispatched to, or ``"unknown"`` — never the raw URL
    path (mirrors Go ``label.HTTPRoute``). Resolved before the handler runs, like Gin's
    ``FullPath()``, so the in-flight gauge and the request counters share one label. Method
    mismatches (405) and redirects count as ``unknown``, as in Gin.

    Matched once per request: the result is kept in the scope (shared by the ASGI layers), so
    the tracing, logging and metrics middleware do not each re-run the route table."""
    cached = scope.get(_ROUTE_SCOPE_KEY)
    if isinstance(cached, str):
        return cached
    router = getattr(scope.get("app"), "router", None)
    routes = getattr(router, "routes", None)
    if not routes:
        return UNKNOWN  # not cached: no app yet (a bare scope), so a later call may resolve
    path = _match_routes(routes, scope, "")
    label = route_template_label(path) if path else UNKNOWN
    scope[_ROUTE_SCOPE_KEY] = label
    return label


#: Scope key of the request's resolved route label (see :func:`http_route_label`).
_ROUTE_SCOPE_KEY = "fastapicommon.route"


def request_body_size(content_length: str | None) -> float:
    """Content-Length when positive, else 0 (Go ``RequestBodySize``)."""
    try:
        size = int(content_length) if content_length is not None else 0
    except ValueError:
        return 0.0
    return float(size) if size > 0 else 0.0


def proto_message_size(message: Any) -> float:
    """Encoded size of a protobuf message, else 0 (Go ``ProtoMessageSize``: non-proto values,
    including raw ``bytes`` handlers, report 0)."""
    byte_size = getattr(message, "ByteSize", None)
    if message is None or not callable(byte_size):
        return 0.0
    return float(byte_size())


# --------------------------------------------------------------------------------------------
# Options / identity resolution (mirror Go Options.resolve + MustInitFromConfig)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricsOptions:
    """Metrics initialization options (Go ``metrics.Options``).

    ``domain`` is required; empty falls back to ``OBSERVABILITY_DOMAIN``, and with neither set
    :func:`resolve_metrics_options` raises ``ValueError``. ``environment`` is required too; empty
    falls back to ``APP_ENV``, then ``ENVIRONMENT``.
    """

    service_name: str
    build_version: str = ""
    domain: str = ""
    environment: str = ""

    @classmethod
    def from_settings(cls, settings: PlatformSettings) -> MetricsOptions:
        # An unset environment is "" (PlatformSettings' default), so it still fails at startup.
        return cls(
            service_name=settings.app_name,
            build_version=settings.build_version,
            domain=settings.observability_domain,
            environment=settings.environment,
        )


@dataclass(frozen=True)
class MetricsIdentity:
    """Resolved, validated identity: the const labels plus ``platform_build_info``'s version."""

    service: str
    version: str
    domain: str
    environment: str

    def value_of(self, label: str) -> str:
        values = {
            "domain": self.domain,
            "service": self.service,
            "environment": self.environment,
            "version": self.version,
        }
        return values[label]


def resolve_metrics_options(
    options: MetricsOptions, registry: ObservabilityRegistry | None = None
) -> MetricsIdentity:
    """Apply the env fallbacks and validate the identity; raises ``ValueError`` on a missing or
    unknown domain, an invalid service name, or an unmappable/missing environment."""
    reg = registry or default_registry()
    domain = configured_domain(options)
    if not domain:
        raise ValueError(
            "metrics: a domain is required — set PlatformSettings.observability_domain or the "
            "OBSERVABILITY_DOMAIN environment variable (or disable metrics with "
            "enable_metrics=False)"
        )
    environment = (
        options.environment or os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT", "")
    )
    version = options.build_version or DEFAULT_VERSION
    try:
        environment = reg.check_identity(domain, options.service_name, environment)
    except ValueError as exc:
        raise ValueError(f"metrics: {exc}") from exc
    return MetricsIdentity(options.service_name, version, domain, environment)


def configured_domain(options: MetricsOptions) -> str:
    """``options.domain``, else ``OBSERVABILITY_DOMAIN``, else ``""`` (not validated)."""
    return options.domain or os.environ.get("OBSERVABILITY_DOMAIN", "")


# --------------------------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------------------------


#: Families for which prometheus_client adds a ``<family>_created`` timestamp sample.
_CREATED_TYPES = frozenset({"counter", "histogram", "summary"})


def is_created_sample(family: Metric, sample_name: str) -> bool:
    """Whether ``sample_name`` is prometheus_client's ``<family>_created`` timestamp sample of
    ``family`` (Go's client emits none). A metric that is merely *named* ``..._created`` (a
    gauge ``iam_orders_api_items_created``) is not one."""
    return family.type in _CREATED_TYPES and sample_name == f"{family.name}_created"


class _WithoutCreatedSeries(Collector):
    """Exposes the wrapped collectors without prometheus_client's ``*_created`` samples.

    Go's client does not emit them, and a text-format parser (Go ``metricslint check``) would read
    ``platform_http_requests_created`` as an unregistered, untyped ``platform_*`` family.
    """

    def __init__(self, metrics: Iterable[MetricWrapperBase]) -> None:
        self._metrics = tuple(metrics)

    def describe(self) -> Iterator[Metric]:
        for metric in self._metrics:
            yield from metric.describe()

    def collect(self) -> Iterator[Metric]:
        for metric in self._metrics:
            for family in metric.collect():
                family.samples = [
                    s for s in family.samples if not is_created_sample(family, s.name)
                ]
                yield family


#: ``transport`` label values of ``platform_unclassified_errors_total``.
TRANSPORT_HTTP = "http"
TRANSPORT_GRPC = "grpc"


class PlatformMetrics:
    """The ``platform_*`` collectors fastapicommon emits, built from their registry entries."""

    def __init__(self, identity: MetricsIdentity, registry: ObservabilityRegistry) -> None:
        self._registry = registry
        self._const_names = registry.const_labels
        self._const = tuple(identity.value_of(name) for name in self._const_names)
        self._built: list[MetricWrapperBase] = []

        self.build_info = self._gauge("platform_build_info")
        self.http_requests = self._counter("platform_http_requests_total")
        self.http_duration = self._histogram("platform_http_request_duration_seconds")
        self.http_request_size = self._histogram("platform_http_request_size_bytes")
        self.http_response_size = self._histogram("platform_http_response_size_bytes")
        self.http_active = self._gauge("platform_http_active_requests")
        self.http_panics = self._counter("platform_http_panics_total")
        self.http_rate_limited = self._counter("platform_http_rate_limited_requests_total")
        self.http_timeouts = self._counter("platform_http_request_timeouts_total")
        self.grpc_handled = self._counter("platform_grpc_server_handled_total")
        self.grpc_duration = self._histogram("platform_grpc_server_handling_seconds")
        self.grpc_request_size = self._histogram("platform_grpc_server_request_size_bytes")
        self.grpc_response_size = self._histogram("platform_grpc_server_response_size_bytes")
        self.grpc_inflight = self._gauge("platform_grpc_server_inflight_requests")
        self.grpc_rate_limited = self._counter("platform_grpc_rate_limited_requests_total")
        self.grpc_panics = self._counter("platform_grpc_panics_total")
        self.unclassified_errors = self._counter("platform_unclassified_errors_total")
        # Both series exist from startup (at 0), so increase() sees the first error.
        for transport in (TRANSPORT_HTTP, TRANSPORT_GRPC):
            self.unclassified_errors.labels(*self._const, transport)

        # platform_build_info's declared labels (e.g. version) are identity values too.
        spec = self._spec("platform_build_info", "gauge")
        self.build_info.labels(*self._const, *(identity.value_of(n) for n in spec.labels)).set(1)

        self.collector = _WithoutCreatedSeries(self._built)

    # -- builders (a missing/mistyped entry is a defect in this package and raises) -------------

    def _spec(self, name: str, metric_type: str) -> MetricSpec:
        spec = self._registry.lookup(name)
        if spec is None or spec.type != metric_type:
            raise RuntimeError(f"metrics: registry has no {metric_type} {name!r}")
        return spec

    def _labelnames(self, labels: Sequence[str]) -> list[str]:
        return [*self._const_names, *labels]

    def _counter(self, name: str) -> Counter:
        spec = self._spec(name, "counter")
        metric = Counter(name, spec.help, self._labelnames(spec.labels), registry=None)
        self._built.append(metric)
        return metric

    def _gauge(self, name: str) -> Gauge:
        spec = self._spec(name, "gauge")
        metric = Gauge(name, spec.help, self._labelnames(spec.labels), registry=None)
        self._built.append(metric)
        return metric

    def _histogram(self, name: str) -> Histogram:
        spec = self._spec(name, "histogram")
        # Shared histograms always name a preset (the registry enforces it); without one Go
        # falls back to the client's default buckets, and so does this.
        buckets = (
            self._registry.buckets(spec.buckets) if spec.buckets else Histogram.DEFAULT_BUCKETS
        )
        metric = Histogram(
            name, spec.help, self._labelnames(spec.labels), buckets=buckets, registry=None
        )
        self._built.append(metric)
        return metric

    # -- HTTP ----------------------------------------------------------------------------------

    def http_active_inc(self, route: str) -> None:
        self.http_active.labels(*self._const, route).inc()

    def http_active_dec(self, route: str) -> None:
        self.http_active.labels(*self._const, route).dec()

    def record_http(
        self,
        method: str,
        route: str,
        status_code: int,
        seconds: float,
        request_size: float,
        response_size: float | None,
    ) -> None:
        """One completed request. ``response_size=None`` defers that observation to
        :meth:`observe_http_response_size` (body still streaming)."""
        labels = (
            *self._const,
            http_method_label(method),
            route,
            http_status_class(status_code),
            http_error_class(status_code),
        )
        self.http_requests.labels(*labels).inc()
        self.http_duration.labels(*labels).observe(seconds)
        self.http_request_size.labels(*labels).observe(request_size)
        if response_size is not None:
            self.http_response_size.labels(*labels).observe(response_size)
        if status_code == 429:
            self.record_http_rate_limited(method, route)

    def observe_http_response_size(
        self, method: str, route: str, status_code: int, size: float
    ) -> None:
        self.http_response_size.labels(
            *self._const,
            http_method_label(method),
            route,
            http_status_class(status_code),
            http_error_class(status_code),
        ).observe(size)

    def record_http_panic(self, method: str, route: str) -> None:
        self.http_panics.labels(*self._const, http_method_label(method), route).inc()

    def record_http_rate_limited(self, method: str, route: str) -> None:
        self.http_rate_limited.labels(*self._const, http_method_label(method), route).inc()

    def record_http_timeout(self, method: str, route: str) -> None:
        self.http_timeouts.labels(*self._const, http_method_label(method), route).inc()

    # -- gRPC ----------------------------------------------------------------------------------

    def grpc_inflight_inc(self, grpc_method: str) -> None:
        self.grpc_inflight.labels(*self._const, grpc_method).inc()

    def grpc_inflight_dec(self, grpc_method: str) -> None:
        self.grpc_inflight.labels(*self._const, grpc_method).dec()

    def record_grpc(
        self,
        grpc_method: str,
        grpc_code: str,
        seconds: float,
        request_size: float,
        response_size: float,
    ) -> None:
        status_class = grpc_status_class(grpc_code)
        error_class = grpc_error_class(grpc_code)
        self.grpc_handled.labels(
            *self._const, grpc_method, grpc_code, status_class, error_class
        ).inc()
        outcome = (*self._const, grpc_method, status_class, error_class)
        self.grpc_duration.labels(*outcome).observe(seconds)
        self.grpc_request_size.labels(*outcome).observe(request_size)
        self.grpc_response_size.labels(*outcome).observe(response_size)
        if grpc_code == "ResourceExhausted":
            self.grpc_rate_limited.labels(*self._const, grpc_method).inc()

    def record_grpc_panic(self, grpc_method: str) -> None:
        self.grpc_panics.labels(*self._const, grpc_method).inc()

    def record_unclassified_error(self, transport: str) -> None:
        """``platform_unclassified_errors_total{transport}`` +1 (Go
        ``metrics.RecordUnclassifiedError``): an exception that reached the top-level handler
        unclassified and was answered as a generic internal error."""
        self.unclassified_errors.labels(*self._const, transport).inc()


# --------------------------------------------------------------------------------------------
# Process-wide state
# --------------------------------------------------------------------------------------------


@dataclass
class _State:
    initialized: bool = False
    registry: CollectorRegistry = REGISTRY
    platform: PlatformMetrics | None = None
    identity: MetricsIdentity | None = None


_state = _State()
_lock = threading.Lock()


def init_metrics(options: MetricsOptions, *, registry: CollectorRegistry | None = None) -> None:
    """Initialize metrics under the observability standard (Go ``MustInitFromConfig``).

    Raises ``ValueError`` on a missing domain or an identity the registry rejects. Without
    ``registry`` the first successful call in the process wins (later calls only validate) and
    collectors go on prometheus_client's default ``REGISTRY``. With ``registry`` (tests) the
    active collectors are replaced and registered there.
    """
    identity = resolve_metrics_options(options)
    with _lock:
        if registry is None and _state.initialized:
            return
        target = registry if registry is not None else REGISTRY
        platform = PlatformMetrics(identity, default_registry())
        target.register(platform.collector)
        _state.initialized = True
        _state.registry = target
        _state.platform = platform
        _state.identity = identity


def init_metrics_from_settings(
    settings: PlatformSettings, *, registry: CollectorRegistry | None = None
) -> None:
    """:func:`init_metrics` from ``PlatformSettings`` (``app_name``, ``build_version``,
    ``observability_domain``, ``environment``) when ``settings.enable_metrics`` is set.

    With metrics disabled nothing is initialized and no domain is needed; a domain that *is*
    configured is still validated, so a misconfigured identity fails at startup rather than when
    metrics are re-enabled.
    """
    options = MetricsOptions.from_settings(settings)
    if settings.enable_metrics:
        init_metrics(options, registry=registry)
    elif configured_domain(options):
        resolve_metrics_options(options)


def platform_metrics() -> PlatformMetrics | None:
    """The active ``platform_*`` collectors, or ``None`` before :func:`init_metrics` (metrics
    disabled)."""
    return _state.platform


def platform_metrics_enabled() -> bool:
    return _state.platform is not None


def metrics_identity() -> MetricsIdentity | None:
    """Identity of the active initialization, or ``None`` before :func:`init_metrics`."""
    return _state.identity


def metrics_registry() -> CollectorRegistry:
    """The registry ``/metrics`` serves: the one passed to the active :func:`init_metrics` call,
    else prometheus_client's default ``REGISTRY``."""
    return _state.registry
