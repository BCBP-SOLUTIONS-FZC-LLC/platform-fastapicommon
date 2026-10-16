# ADR-001: Logging

## Status

Accepted; amended 2026-10-16 (output contract aligned with platform-gincommon).

## Context

Platform services need consistent, machine-parseable logs so log shipping, indexing, and
correlation work the same way regardless of which service produced them — including across
languages: Go services log through platform-gincommon's Zap logger.

## Decision

Standardize on [structlog](https://www.structlog.org/) for all logging, rendering
platform-gincommon's output contract:

- JSON on stderr in every environment except `dev` / `local`, in Zap's production layout
  (`level`, `ts`, `caller`, `msg`, the fields, `stacktrace` on errors);
- Zap's console layout in `dev` / `local` (the original decision was JSON everywhere; it was
  changed so a developer reads the same thing in a Go and a Python service);
- `LOG_LEVEL`, `LOG_SAMPLING` and `LOG_FILE` with gincommon's semantics;
- gincommon's log events (`http_request`, `grpc_request`, `auth: request rejected`, ...) with
  the same field names, and its sanitising (sensitive keys redacted, header-derived values
  stripped of control characters and capped at 256 bytes).

Every record written inside a request is enriched with `request_id`, `correlation_id`,
`trace_id`, `tenant_id` and `user_id` from the active `RequestContext`.

## Consequences

- Loki queries, dashboards and alerts written for Go services work unchanged for Python ones.
- Local development logs are human-readable; set `APP_ENV` to anything but `dev` / `local` to
  see the production JSON.
- The stdlib root logger is routed through the same formatter, so third-party and stdlib
  `logging` records share the layout; platform code should still prefer
  `fastapicommon.logging.get_logger`.
