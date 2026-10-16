from fastapicommon.metrics.api import (
    MetricsNotInitializedError,
    identity_labels,
    new_counter,
    new_gauge,
    new_histogram,
    new_shared_counter,
    new_shared_gauge,
    new_shared_histogram,
    register_labeled,
)
from fastapicommon.metrics.middleware import PrometheusMiddleware
from fastapicommon.metrics.platform import (
    MetricsIdentity,
    MetricsOptions,
    PlatformMetrics,
    http_method_label,
    init_metrics,
    init_metrics_from_settings,
    metrics_identity,
    metrics_registry,
    platform_metrics,
    platform_metrics_enabled,
    resolve_metrics_options,
)
from fastapicommon.metrics.registry import ObservabilityRegistry, default_registry
from fastapicommon.metrics.router import metrics_router

__all__ = [
    "MetricsIdentity",
    "MetricsNotInitializedError",
    "MetricsOptions",
    "ObservabilityRegistry",
    "PlatformMetrics",
    "PrometheusMiddleware",
    "default_registry",
    "http_method_label",
    "identity_labels",
    "init_metrics",
    "init_metrics_from_settings",
    "metrics_identity",
    "metrics_registry",
    "metrics_router",
    "new_counter",
    "new_gauge",
    "new_histogram",
    "new_shared_counter",
    "new_shared_gauge",
    "new_shared_histogram",
    "platform_metrics",
    "platform_metrics_enabled",
    "register_labeled",
    "resolve_metrics_options",
]
