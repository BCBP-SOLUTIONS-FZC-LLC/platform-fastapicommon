# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `0.3.x` (current) | Yes — security fixes released as patch versions |
| `< 0.3.0` | No — upgrade to `0.3.x` (README *Upgrading to 0.3.0*) |

While the package is `0.y.z`, only the latest MINOR line receives fixes. Once `1.0.0` ships, this follows platform-gincommon's policy: when a new major line ships, the previous major receives security fixes only for six months after the new major's release.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues by email to **vijay@bcbpsolutions.com** with the subject line `[platform-fastapicommon] Security vulnerability`.

Include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce or a proof-of-concept (a minimal pytest case or `curl` / `grpcurl` sequence is sufficient).
- The version(s) affected.
- Any suggested fix, if you have one.

You will receive an acknowledgement within **2 business days** and a resolution timeline within **5 business days**.

## Trust model and known limitations

This library is designed to run **behind a trusted API gateway** that authenticates users and injects identity headers (`x-user-id`, `x-tenant-id`, `x-tenant-roles`). It validates header format and ambiguity on HTTP and gRPC, but does **not** cryptographically verify identity.

| Assumption | Implication |
|-----------|-------------|
| API gateway strips client-supplied identity headers and **replaces** them | If a service is exposed directly (no gateway), any client can impersonate any user by setting `x-user-id`. A gateway that appends instead of replacing makes the identity ambiguous: a repeated `x-user-id` / `x-tenant-id` makes the whole identity absent and `require_authenticated_user` answers 401 (`Ambiguous x-user-id header.`), so a client value never wins |
| `x-tenant-roles` is set by the gateway after authentication | If the gateway forwards client-supplied role headers, privilege escalation through `RequireRole` / `RequireAnyRole` / `RequireAllRoles` is possible. Roles are capped (4096 bytes, 64 roles; else `401 Invalid tenant roles.`) and tokens outside `[a-zA-Z0-9_-]+` are dropped |
| **There is no global authentication middleware** | Unlike gincommon's `RequireAuth` chains, a route is anonymous unless it declares `Depends(require_authenticated_user)` or a role dependency. On gRPC there are no auth interceptors: handlers check `get_request_context()` themselves |
| The role dependencies are coarse checks | Tenant- or resource-scoped authorization is the service's, built on the validated `RequestContext` |
| The client IP is the socket peer unless `TRUSTED_PROXIES` is set | `X-Forwarded-For` / `X-Real-IP` are believed only when the peer is a listed proxy (Gin's `SetTrustedProxies` algorithm). Behind a proxy or load balancer, list its CIDRs, or the recorded client IP is the proxy's |
| `create_platform_http_client()` forwards identity to any host | Use it only for internal platform services; call third parties with a plain client (LLD OQ-6) |
| `/metrics` is served without authentication | Restrict access to `/metrics` via network policy, an auth proxy, or a dedicated admin port in production |
| Only the library's own log entries are sanitized | `http_request`, `grpc_request`, the auth / authz entries and `unclassified error` redact sensitive keys, strip control characters and cap values; `x-request-id` / `x-correlation-id` are validated before they are echoed, logged or propagated. Fields a service logs itself are written as given |

## Scope

The following are in scope for vulnerability reports:

- Authentication bypass in `require_authenticated_user`, or identity accepted on gRPC that HTTP rejects
- Privilege escalation through `RequireRole` / `RequireAnyRole` / `RequireAllRoles`
- Request context leaking between concurrent requests or RPCs (`contextvars` isolation in `fastapicommon.context`, the gRPC interceptors' reset)
- Header injection or log injection in `ContextMiddleware`, `LoggingMiddleware` or the gRPC interceptors
- Sensitive data leakage: an exception message, stack source, or unverified identity reaching a client, a log, a span or a metric label
- Client-IP spoofing past `TRUSTED_PROXIES`
- Denial-of-service conditions in any middleware or interceptor (including metric label cardinality)

Out of scope: theoretical attacks that require the attacker to already control the API gateway, issues in transitive dependencies unrelated to this library's functionality, or issues in the `examples/` demo services.
