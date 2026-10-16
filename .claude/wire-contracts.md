# Wire Contracts

fastapicommon owns **no database and no persisted state**. What it owns instead is everything that crosses the process boundary, and every item here must match platform-gincommon v2.0.0, because Go and Python services are read by the same gateway, clients, dashboards and log queries. This file replaces the siblings' `database-schema.md`. The long form is LLD §4 (contracts) and §7 (telemetry); the error contract is [`docs/problem-details.md`](../docs/problem-details.md).

## Inbound Headers (HTTP) / Metadata (gRPC)

| Header | Rule (both transports) | When invalid |
|---|---|---|
| `x-user-id`, `x-tenant-id` | trimmed, non-empty, ≤ 256 bytes, no ASCII control / DEL / U+2028 / U+2029; **exactly one value** | invalid → that field absent; **repeated** → whole identity absent, `identity_rejection = "ambiguous x-user-id header"` (or tenant) |
| `x-tenant-roles` | every value joined with `,`; tokens `[a-zA-Z0-9_-]+` ≤ 64 bytes (others dropped), deduplicated; joined header ≤ 4096 bytes and ≤ 64 roles | over a cap → whole identity absent, `identity_rejection = "invalid tenant roles"` |
| `x-request-id` | `[A-Za-z0-9._-]`, ≤ 128 bytes | a new UUID |
| `x-correlation-id` | same rule | the request id |
| `traceparent` (+ `baggage` when `OTEL_PROPAGATOR_BAGGAGE`) | W3C | a new root trace |
| `X-Forwarded-For`, `X-Real-IP` | believed only when the socket peer is in `TRUSTED_PROXIES`; XFF walked from the right to the first untrusted hop; all trusted → leftmost; invalid hop ends the walk; a non-IP peer is never trusted | the client IP is the peer |

gRPC `-bin` metadata (bytes) is never read as an id or identity. `require_authenticated_user` turns a rejection into a 401 with gincommon's detail: `Ambiguous x-user-id header.`, `Ambiguous x-tenant-id header.`, `Invalid tenant roles.`, or `Missing or invalid authentication headers.`.

## Response Headers

`X-Request-ID` (and lowercase `x-request-id`), `x-correlation-id`, `X-Trace-ID` (when a span exists), on every HTTP response including errors.

## Outbound Propagation

| Client | Sends |
|---|---|
| `create_platform_http_client()` | `traceparent` (+ baggage), `x-request-id`, `x-correlation-id`, `x-user-id`, `x-tenant-id`, `x-tenant-roles` — to **any** host (internal services only, OQ-6) |
| `ContextClientInterceptor` (+ `instrument_grpc_client()` for `traceparent`) | ids and identity, no roles; unary calls only (OQ-7) |

## RFC 9457 Problem Types

`application/problem+json`; `type = {PROBLEM_TYPE_BASE}/{domain}/{name}` (default base `https://errors.company.com`, domain `platform`). Body: `type`, `title`, `status`, `detail` (never on 5xx), `instance` (path without query), `traceId` (`traceparent` → `x-request-id` → span → generated), `errorCode`, `tenantId` (verified only, never on a 401), `timestamp`, `validationErrors` (`[{field, message}]`, never the rejected value).

| name | status | errorCode | title |
|---|---|---|---|
| `bad-request` | 400 | `BAD_REQUEST` | Bad Request |
| `validation-failed` | 400 | `VALIDATION_FAILED` | Validation failed |
| `unauthorized` | 401 | `UNAUTHORIZED` | Unauthorized |
| `forbidden` | 403 | `FORBIDDEN` | Forbidden |
| `not-found` | 404 | `NOT_FOUND` (`ROUTE_NOT_FOUND` for an unmatched route) | Not Found |
| `method-not-allowed` | 405 | `METHOD_NOT_ALLOWED` | Method Not Allowed |
| `conflict` | 409 | `CONFLICT` | Conflict |
| `payload-too-large` | 413 | `PAYLOAD_TOO_LARGE` | Payload Too Large |
| `unsupported-media-type` | 415 | `UNSUPPORTED_MEDIA_TYPE` | Unsupported Media Type |
| `business-rule-violation` | 422 | `BUSINESS_RULE_VIOLATION` | Business rule violation |
| `rate-limit-exceeded` | 429 | `RATE_LIMIT_EXCEEDED` | Too Many Requests |
| `internal-error` | 500 | `INTERNAL_ERROR` | Internal Server Error |
| `not-implemented` | 501 | `NOT_IMPLEMENTED` | Not Implemented |
| `service-unavailable` | 503 | `SERVICE_UNAVAILABLE` (`REQUEST_TIMEOUT` from `TimeoutMiddleware`) | Service Unavailable |
| `gateway-timeout` | 504 | `GATEWAY_TIMEOUT` | Gateway Timeout |

gRPC equivalent: a status the handler chose is kept; anything else is `INTERNAL "internal server error"` (`MSG_INTERNAL`), never grpcio's `UNKNOWN "Exception calling application: <message>"`.

## Log Events (Zap layout, stderr)

Keys `level` (`debug|info|warn|error`), `ts` (Unix seconds; ISO8601 in `LOG_FILE`), `caller` (`dir/file.py:line`, omitted when unknown), `msg`, fields, `stacktrace` (error in production, warn in dev). Every entry in a request carries `request_id`, `correlation_id`, `trace_id`, and verified `tenant_id` / `user_id`.

| `msg` | Level | Notable fields |
|---|---|---|
| `http_request` | info 1xx–3xx · warn 4xx · error 5xx · debug when aborted | `method`, `path` (route template), `status`, `duration_ms` (float ms), verified identity or `claimed_*`, `aborted` |
| `grpc_request` | info OK · warn client codes · error otherwise | `method` (bounded), `grpc_code` (Go names), `duration_ms` |
| `auth: request rejected` | warn | `reason`, `method`, `route`, `status`, `client_ip`, `claimed_user_id`, `claimed_tenant_id` |
| `authz: access denied` | warn | `roles`, `action` (`require_role:<r>`), `resource` |
| `problem response` | warn | `traceId`, `type`, `status`, `errorCode`, `service`, `tenantId` |
| `unclassified error` | error | `classified=false`, `error_type`, `error_chain` (≤ 8), `stack` (≤ 30 frames); **never the message** |
| `rate limited` | warn, ≤ 1/route/s | `route`, `method`, `status`, `suppressed` |
| `metrics handler error`, `otel_*` | error / info / warn | `error`, configuration |

## Metrics (registry-defined)

Names, help, label order and buckets come from `src/fastapicommon/metrics/registry.json` (gincommon's, byte-identical). Const labels `domain`, `service`, `environment` on every series, injected centrally; `version` only on `platform_build_info`. Label values are bounded: `method` (standard methods, else `OTHER`), `route` (Gin template or `unknown`), `status_class` / `error_class` (`2xx`/`success`, `4xx`/`client_error`, `5xx`/`server_error`), `grpc_code` (Go names), `grpc_method` (`/pkg.Service/Method` ≤ 256 bytes, else `unknown`). A cancelled request is 499 / `client_error`; an exception after the response started keeps the status sent; neither is a panic. Response size = body bytes sent. Prohibited labels (`tenant_id`, `user_id`, …) fail compliance. Inventory: [`docs/observability/metric-registry.md`](../docs/observability/metric-registry.md).

## Spans

HTTP server span `METHOD route` (`GET /items/:item_id`) with `http.request.method`, `http.route`, `url.path`, `user_agent.original`, `server.address`, `client.address` (trusted-proxy resolution), `http.response.status_code`; Error status for 5xx (description = the status phrase; none for a non-standard code), `panic` for an escaped exception, `http.aborted` (not Error) when aborted. gRPC span named after the bounded method with `rpc.*` and both status-code attributes. Exceptions recorded as an `exception` event with `exception.type` (`module.Class`) only. Resource: `service.name`, `service.version`, `service.namespace` (domain), `deployment.environment.name`.
