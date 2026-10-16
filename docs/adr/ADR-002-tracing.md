# ADR-002: Tracing

## Status

Accepted; amended 2026-10-16 (aligned with platform-gincommon's tracing).

## Context

Requests cross multiple services (HTTP and gRPC), in Go and Python, and need end-to-end
visibility without each service inventing its own tracing approach.

## Decision

Adopt [OpenTelemetry](https://opentelemetry.io/) as the platform tracing standard and follow
platform-gincommon's tracing contract exactly:

- the same `OTEL_*` configuration and defaults (OTLP/gRPC to `localhost:4317`, plaintext only in
  dev/local, parent-based sampling at 1.0 in dev/local and 0.1 otherwise);
- the same resource (`service.name`, `service.version`, `service.namespace`,
  `deployment.environment.name`);
- library-owned server spans — `TracingMiddleware` (HTTP) and `ContextServerInterceptor` (gRPC)
  — with gincommon's names (`METHOD route`, bounded `grpc_method`), attributes and status rules,
  instead of OpenTelemetry's generic FastAPI / gRPC server instrumentation, whose span names and
  attributes differ;
- one global provider set once (delegating), so later installs swap the SDK provider safely.

Trace context propagates via the W3C `traceparent` header, alongside `x-request-id` and
`x-correlation-id` for log correlation.

## Consequences

- Traces from Go and Python services look the same in Tempo (service map, span names, SLO
  queries on span attributes).
- `opentelemetry-instrumentation-fastapi` is no longer a dependency;
  `opentelemetry-instrumentation-grpc` remains for `instrument_grpc_client()`.
- Services can plug in any OTLP-compatible collector/backend without library changes.
