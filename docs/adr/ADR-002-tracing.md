# ADR-002: Tracing

## Status

Accepted

## Context

Requests cross multiple services (HTTP and gRPC) and need end-to-end visibility without
each service inventing its own tracing approach.

## Decision

Adopt [OpenTelemetry](https://opentelemetry.io/) as the platform tracing standard for
both FastAPI (HTTP) and gRPC services. `fastapicommon.telemetry.setup_tracing`
instruments the FastAPI app; `instrument_grpc_server` / `instrument_grpc_client`
instrument gRPC. Trace context propagates via the standard `traceparent` header, alongside
`x-request-id` and `x-correlation-id` for log correlation.

## Consequences

- Services can plug in any OTLP-compatible collector/backend without library changes.
- `x-request-id` / `x-correlation-id` remain the correlation keys for logs, while
  `traceparent` remains the source of truth for span linkage — the two are
  complementary, not redundant.
- gRPC instrumentation requires the `fastapicommon[grpc]` extra.
