"""Metrics API for consuming services' own collectors (Go ``pkg/gincommon/metrics_api.go``).

Collectors built here carry the identity const labels (``domain``, ``service``,
``environment``) of the active :func:`~fastapicommon.metrics.init_metrics` call and register on
the registry ``/metrics`` serves, so one scrape serves fastapicommon's metrics and the service's.
The Enterprise Platform Observability Standard is enforced at construction:

- ``new_shared_*`` build Tier 1 (``platform_*``) and Tier 2 (``<domain>_*``) metrics from their
  Platform Observability Registry entry: name, help, label names (in registry order) and buckets
  are the registry's, so services cannot drift from the contract. A domain metric can only be
  built by a service in that domain.
- ``new_counter`` / ``new_gauge`` / ``new_histogram`` build Tier 3 service-specific metrics.
  They reject registry and ``platform_*`` names, wrong suffixes (``_total`` for counters,
  ``_seconds`` for histograms, no ``_total`` on gauges), prohibited high-cardinality labels and
  the centrally injected labels, and the name must start with the service's
  ``<domain>_<service>_`` prefix.

Every constructor raises :class:`MetricsNotInitializedError` before metrics are initialized
(``create_platform_app()`` or ``ContextServerInterceptor(settings)``): the identity labels are
not known yet. The returned objects are plain ``prometheus_client`` metrics; the identity labels
are added when they are collected, so ``.labels(...)`` takes only the metric's own labels::

    processed = new_shared_counter("platform_messages_processed_total")
    processed.labels(queue="iam.events", event_type="user.created").inc()

    lookups = new_histogram(
        "iam_event_consumer_tenant_lookup_seconds", "Tenant lookup latency", ["cache"]
    )
    lookups.labels(cache="hit").observe(0.004)
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TypeVar

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector
from prometheus_client.samples import Sample

from fastapicommon.metrics.platform import metrics_identity, metrics_registry
from fastapicommon.metrics.registry import (
    COUNTER,
    GAUGE,
    HISTOGRAM,
    MetricSpec,
    default_registry,
)


class MetricsNotInitializedError(RuntimeError):
    """A metric was built before the identity labels were known."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"metric {name!r}: metrics are not initialized yet; create the app with "
            "create_platform_app() (or ContextServerInterceptor(settings), or call init_metrics) "
            "first"
        )


class IdentityLabeled(Collector):
    """Exposes a collector's families with the identity const labels added to every sample
    (and prometheus_client's ``*_created`` samples dropped, as Go's client emits none).

    The labels are read when the collector is collected, so the wrapper may be created before
    :func:`~fastapicommon.metrics.init_metrics` runs (it then exposes the samples unchanged).
    Go ``LabeledRegisterer``; do not wrap collectors that inject the identity labels themselves
    (platform-eventcommon, platform-pgcommon-py), or the labels are duplicated.
    """

    def __init__(self, collector: Collector) -> None:
        self._collector = collector

    def describe(self) -> list[Metric]:
        describe = getattr(self._collector, "describe", None)
        return list(describe()) if describe is not None else []

    def collect(self) -> Iterator[Metric]:
        const = identity_labels()
        for family in self._collector.collect():
            family.samples = [
                Sample(s.name, {**const, **s.labels}, s.value, s.timestamp, s.exemplar)
                for s in family.samples
                if not s.name.endswith("_created")
            ]
            yield family


def identity_labels() -> dict[str, str]:
    """The identity const labels (``domain``, ``service``, ``environment``) of the active
    initialization, ``{}`` before it (Go ``MetricsConstLabels``)."""
    identity = metrics_identity()
    if identity is None:
        return {}
    return {name: identity.value_of(name) for name in default_registry().const_labels}


def register_labeled(collector: Collector, registry: CollectorRegistry | None = None) -> Collector:
    """Register a third-party collector with the identity labels added to its samples (Go
    ``LabeledRegisterer``). ``registry`` defaults to the one ``/metrics`` serves. Returns the
    registered wrapper (pass it to ``registry.unregister`` to remove it)."""
    wrapper = IdentityLabeled(collector)
    (registry if registry is not None else metrics_registry()).register(wrapper)
    return wrapper


# -- Tier 3: service-specific ---------------------------------------------------------------------


def new_counter(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    *,
    registry: CollectorRegistry | None = None,
) -> Counter:
    """A service-specific counter (``<domain>_<service>_<metric>_total``), registered on
    ``registry`` (default: the one ``/metrics`` serves). Go ``NewCounterVec`` / ``NewCounter``."""
    _check_custom(COUNTER, name, labelnames)
    return _register(Counter(name, documentation, labelnames, registry=None), registry)


def new_gauge(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    *,
    registry: CollectorRegistry | None = None,
) -> Gauge:
    """A service-specific gauge (Go ``NewGaugeVec`` / ``NewGauge``)."""
    _check_custom(GAUGE, name, labelnames)
    return _register(Gauge(name, documentation, labelnames, registry=None), registry)


def new_histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    *,
    buckets: Sequence[float] | None = None,
    registry: CollectorRegistry | None = None,
) -> Histogram:
    """A service-specific histogram (name ending in ``_seconds``); ``buckets=None`` means the
    registry's ``request_duration`` preset (Go ``NewHistogramVec``)."""
    _check_custom(HISTOGRAM, name, labelnames)
    resolved = (
        tuple(buckets) if buckets is not None else default_registry().buckets("request_duration")
    )
    metric = Histogram(name, documentation, labelnames, buckets=resolved, registry=None)
    return _register(metric, registry)


# -- Tier 1 / 2: shared, from the registry entry ------------------------------------------------


def new_shared_counter(name: str, *, registry: CollectorRegistry | None = None) -> Counter:
    """The registry counter ``name`` (e.g. ``platform_messages_processed_total`` or, in domain
    ``iam``, ``iam_auth_events_total``) with the registry's help and label names (Go
    ``NewSharedCounterVec``)."""
    spec = _shared_entry(name, COUNTER)
    return _register(Counter(spec.name, spec.help, spec.labels, registry=None), registry)


def new_shared_gauge(name: str, *, registry: CollectorRegistry | None = None) -> Gauge:
    """The registry gauge ``name`` (Go ``NewSharedGaugeVec``)."""
    spec = _shared_entry(name, GAUGE)
    return _register(Gauge(spec.name, spec.help, spec.labels, registry=None), registry)


def new_shared_histogram(name: str, *, registry: CollectorRegistry | None = None) -> Histogram:
    """The registry histogram ``name`` with the registry's bucket preset, so quantiles aggregate
    across services (Go ``NewSharedHistogramVec``)."""
    spec = _shared_entry(name, HISTOGRAM)
    buckets = (
        default_registry().buckets(spec.buckets) if spec.buckets else Histogram.DEFAULT_BUCKETS
    )
    metric = Histogram(spec.name, spec.help, spec.labels, buckets=buckets, registry=None)
    return _register(metric, registry)


# -- helpers ------------------------------------------------------------------------------------


def _check_custom(metric_type: str, name: str, labelnames: Sequence[str]) -> None:
    identity = metrics_identity()
    if identity is None:
        raise MetricsNotInitializedError(name)
    default_registry().check_custom(
        metric_type, name, tuple(labelnames), identity.domain, identity.service
    )


def _shared_entry(name: str, metric_type: str) -> MetricSpec:
    identity = metrics_identity()
    if identity is None:
        raise MetricsNotInitializedError(name)
    return default_registry().shared_metric(name, metric_type, identity.domain)


_M = TypeVar("_M", Counter, Gauge, Histogram)


def _register(metric: _M, registry: CollectorRegistry | None) -> _M:
    target = registry if registry is not None else metrics_registry()
    target.register(IdentityLabeled(metric))
    return metric
