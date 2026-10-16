# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `0.x` (current) | Yes — security fixes released as patch versions |

Once `1.0.0` ships, this table will adopt the same policy as `platform-gincommon`: when a
new major line (e.g. `2.x`) ships, the previous major receives security fixes only for six
months after the new major's release.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues by email to **vijay@bcbpsolutions.com** with the subject line
`[platform-fastapicommon] Security vulnerability`.

Include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce or a proof-of-concept (a minimal pytest case or `curl` sequence is
  sufficient).
- The version(s) affected.
- Any suggested fix, if you have one.

You will receive an acknowledgement within **2 business days** and a resolution timeline
within **5 business days**.

## Trust model and known limitations

This library is designed to run **behind a trusted API gateway** that authenticates users
and injects identity headers (`x-user-id`, `x-tenant-id`, `x-tenant-roles`).
`ContextMiddleware` reads these headers **verbatim** — it does not cryptographically
verify identity, and unlike some comparable Go middleware stacks, it does not reject
requests missing these headers either (a request with no identity headers simply gets an
anonymous `CurrentUser`).

| Assumption | Implication |
|-----------|-------------|
| API gateway strips client-supplied identity headers before forwarding | If a service is exposed directly (no gateway), any client can impersonate any user by setting `x-user-id` |
| `x-tenant-roles` is set by the gateway after authentication | If the gateway forwards client-supplied role headers, `RequireRole`/`RequireAnyRole`/`RequireAllRoles` privilege escalation is possible |
| **There is no global "reject unauthenticated requests" middleware** | `fastapicommon.middleware.ContextMiddleware` never rejects a request for missing identity headers. Route-level enforcement is opt-in via `Depends(require_authenticated_user)` or an RBAC dependency — a route that forgets to add one is reachable anonymously |
| Identity headers must arrive exactly once | A repeated `x-user-id` / `x-tenant-id`, or an `x-tenant-roles` over 4096 bytes / 64 roles (repeated role headers are joined), makes the whole identity absent on HTTP and gRPC; `require_authenticated_user` answers 401 (`Ambiguous x-user-id header.` / `Ambiguous x-tenant-id header.` / `Invalid tenant roles.`), as platform-gincommon does |
| The client IP is the socket peer unless `TRUSTED_PROXIES` is set | `X-Forwarded-For` / `X-Real-IP` are believed only when the peer is a listed proxy (Gin `SetTrustedProxies`), so clients cannot spoof the logged / traced `client_ip`. Behind a proxy or load balancer, list its CIDRs, or the recorded client IP is the proxy's |
| `/metrics` is served without authentication | `fastapicommon.metrics.metrics_router` has no auth dependency. Restrict access via network policy, an auth proxy, or a dedicated admin port in production |
| Only the library's own log entries are sanitized | `http_request`, `grpc_request`, the auth / authz entries and `unclassified error` go through `fastapicommon.logging.sanitize` (gincommon's sanitizer: sensitive keys redacted, control characters stripped, values capped). `x-request-id` and `x-correlation-id` are validated (`[A-Za-z0-9._-]`, at most 128 bytes; on HTTP and gRPC) before they are echoed, logged or propagated: an invalid request id is replaced by a new UUID, an invalid correlation id by the request id. Fields a service logs itself are written as given: do not treat log output as safe to render without escaping |

## Scope

The following are in scope for vulnerability reports:

- Privilege escalation through `fastapicommon.rbac.RequireRole` / `RequireAnyRole` /
  `RequireAllRoles`
- Request context leaking between concurrent requests (a `contextvars` isolation bug in
  `fastapicommon.context`)
- Denial-of-service conditions in `fastapicommon.middleware.TimeoutMiddleware` or
  `fastapicommon.middleware.ContextMiddleware`
- Header or log injection made possible by this library's own code (as opposed to the
  documented limits of sanitization above)
- Sensitive data leakage from `fastapicommon.exceptions`' generic exception handler (e.g.
  leaking exception internals in the 500 response body)

Out of scope: theoretical attacks that require the attacker to already control the API
gateway, issues in transitive dependencies unrelated to this library's functionality, or
issues in the `examples/` demo services.
