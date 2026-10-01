# ADR-003: Metrics

## Status

Accepted (revised: the original pre-standard baseline metric set is superseded by the
Enterprise Platform Observability Standard and has been removed)

## Context

Every platform service should expose the same request metrics so dashboards and alerts work
uniformly across the fleet — and across languages, since Go (`platform-gincommon`) and Python
services sit behind the same dashboards.

## Decision

Standardize on [prometheus_client](https://github.com/prometheus/client_python) and the
`platform_*` metrics of the Enterprise Platform Observability Standard, exactly as the Go
original (`platform-gincommon`, the source of truth) emits them. Names, help, label names and
order, label vocabularies and buckets come from the Platform Observability Registry, vendored
byte-for-byte at `src/fastapicommon/metrics/registry.json`, and every series carries the const
labels `domain`, `service` and `environment`. These are the **only** HTTP/gRPC metrics the
library emits.

- HTTP metrics (`platform_http_*`) are recorded by `fastapicommon.metrics.PrometheusMiddleware`
  (timeouts by `TimeoutMiddleware`); the `route` label is the matched route template in
  Gin-style syntax or `unknown`, never the raw URL, so cardinality stays bounded.
- gRPC metrics (`platform_grpc_*`) are recorded by `ContextServerInterceptor` in
  `fastapicommon.grpc`.
- `/metrics` is exposed via `fastapicommon.metrics.metrics_router`.
- The registry domain (`PlatformSettings.observability_domain` / `OBSERVABILITY_DOMAIN`) is
  **mandatory** while metrics are enabled. A missing domain or an invalid identity (unknown
  domain, invalid service label, missing or out-of-vocabulary environment) raises `ValueError`
  at startup — where Go panics — rather than producing series that break cross-service
  aggregation. With `enable_metrics=False` no domain is needed.

The library's earlier, pre-standard baseline metrics (with no const labels and a raw-path
fallback label) were removed outright. The standard's compatibility period — emitting the old
and new names in parallel while consumers migrate — was skipped: neither this library nor
`platform-gincommon` had been deployed to production or dev, so no dashboard, alert or
recording rule depended on them. The registry marks those names `removed`, and Go's
`metricslint check` rejects them.

## Consequences

- Dashboards and alert rules can be shared across all platform services, Go and Python,
  without per-service label wrangling.
- Every service must configure a registry domain (or disable metrics); startup fails fast
  otherwise.
- Services must route dynamic path segments through FastAPI path parameters (not manual
  string parsing) so the route template is available for the `route` label.
