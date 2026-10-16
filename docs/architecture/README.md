# Architecture diagrams

Mermaid source files for the diagrams embedded in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md).

Each `.mmd` file is a standalone Mermaid diagram. The corresponding section in
`ARCHITECTURE.md` embeds the content as a fenced code block and carries a `> Source:`
link back here.

| File | Diagram |
|------|---------|
| [`layer-model.mmd`](mermaid/layer-model.mmd) | Layer model: composition, inbound / outbound adapters, error contract, governance, framework-free core |
| [`module-dependencies.mmd`](mermaid/module-dependencies.mmd) | Subpackage import graph (computed with `ast`; dashed: lazy imports) |
| [`bootstrap-flow.mmd`](mermaid/bootstrap-flow.mmd) | Middleware stack composition: what `create_platform_app()` initialises and installs, in which order |
| [`request-flow.mmd`](mermaid/request-flow.mmd) | HTTP request through Tracing → Context → Logging → UnhandledError → Prometheus → Timeout → handler |
| [`grpc-interceptors.mmd`](mermaid/grpc-interceptors.mmd) | `Context(Async)ServerInterceptor` / `ContextClientInterceptor`: spans, context, metrics, sanitizing, aio cancellation |
| [`auth-context-flow.mmd`](mermaid/auth-context-flow.mmd) | Identity resolution: ids, ambiguous identity, role caps, trusted-proxy client IP, `require_authenticated_user` |
| [`rbac-dependency-flow.mmd`](mermaid/rbac-dependency-flow.mmd) | `RequireRole` / `RequireAnyRole` / `RequireAllRoles` decision and audit entry |
| [`unhandled-error-flow.mmd`](mermaid/unhandled-error-flow.mmd) | Unclassified exceptions, failures after the response started, and cancelled requests (aborts) |
| [`exception-handling-flow.mmd`](mermaid/exception-handling-flow.mmd) | RFC 9457: which handler renders which exception, and what every problem carries |
| [`timeout-flow.mmd`](mermaid/timeout-flow.mmd) | `TimeoutMiddleware`: 503 before the response starts, cancellation propagation |
| [`tracing-initialization.mmd`](mermaid/tracing-initialization.mmd) | `ensure_tracing` / `init_tracing` / `TracingConfig.from_env`, the delegating provider, flush and shutdown |
| [`metrics-flow.mmd`](mermaid/metrics-flow.mmd) | Metrics initialisation and identity, HTTP / gRPC recording, `/metrics` exposition |
| [`registry-governance.mmd`](mermaid/registry-governance.mmd) | The vendored gincommon registry, runtime construction and CI enforcement |
| [`observability-stack.mmd`](mermaid/observability-stack.mmd) | How logs, metrics and traces flow into the local `docker-compose.yml` stack |
| [`consuming-service-wiring.mmd`](mermaid/consuming-service-wiring.mmd) | How a consuming service wires `PlatformSettings`, `create_platform_app`, auth, errors, clients, gRPC and metrics |
| [`documentation-assets.mmd`](mermaid/documentation-assets.mmd) | Documentation sources and where they are rendered / checked |
