# ADR-001: Logging

## Status

Accepted

## Context

Platform services need consistent, machine-parseable logs so log shipping, indexing, and
correlation work the same way regardless of which service produced them.

## Decision

Standardize on [structlog](https://www.structlog.org/) for all logging, configured for
JSON output only — no console/pretty renderer, even in local development. Every log
record is enriched with `request_id`, `correlation_id`, `tenant_id`, and `user_id` from
the active `RequestContext` when one is bound.

## Consequences

- Log aggregation and correlation across services work without per-service parsing rules.
- Local development logs are JSON, which is less pleasant to read directly but keeps
  environments consistent; developers can pipe through `jq` or a formatter if desired.
- All platform code must use `fastapicommon.logging.get_logger`, not the stdlib `logging`
  module directly, to guarantee enrichment.
