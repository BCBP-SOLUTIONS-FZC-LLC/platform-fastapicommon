# ADR-003: Metrics

## Status

Accepted

## Context

Every platform service should expose the same baseline request metrics so dashboards and
alerts work uniformly across the fleet.

## Decision

Standardize on [prometheus_client](https://github.com/prometheus/client_python) and a
fixed baseline metric set:

- `http_requests_total{method, path, status_code}`
- `http_request_duration_seconds{method, path}`
- `grpc_requests_total{method, status_code}`
- `grpc_request_duration_seconds{method}`

HTTP metrics are recorded by `fastapicommon.metrics.PrometheusMiddleware` using the
matched route's path template (not the raw URL) to bound cardinality. `/metrics` is
exposed via `fastapicommon.metrics.metrics_router`. gRPC metrics are recorded by the
server interceptor in `fastapicommon.grpc`.

## Consequences

- Dashboards and alert rules can be shared across all platform services without
  per-service label wrangling.
- Services must route dynamic path segments through FastAPI path parameters (not manual
  string parsing) so the route template is available for the `path` label.
