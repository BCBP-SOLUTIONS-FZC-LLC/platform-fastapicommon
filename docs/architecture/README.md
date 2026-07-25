# Architecture diagrams

Mermaid source files for the diagrams embedded in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md).

Each `.mmd` file is a standalone Mermaid diagram. The corresponding section in
`ARCHITECTURE.md` embeds the content as a fenced code block and carries a `> Source:`
link back here.

| File | Diagram |
|------|---------|
| [`module-dependencies.mmd`](mermaid/module-dependencies.mmd) | `fastapicommon.*` module import graph |
| [`bootstrap-flow.mmd`](mermaid/bootstrap-flow.mmd) | How `create_platform_app()` assembles logging, middleware, metrics, tracing, health, and exception handlers |
| [`request-flow.mmd`](mermaid/request-flow.mmd) | Full HTTP request sequence through `ContextMiddleware` → `PrometheusMiddleware` → `TimeoutMiddleware` → handler |
| [`exception-handling-flow.mmd`](mermaid/exception-handling-flow.mmd) | Which handler catches `PlatformException`, `RequestValidationError`, `StarletteHTTPException`, and unhandled exceptions |
| [`rbac-dependency-flow.mmd`](mermaid/rbac-dependency-flow.mmd) | `RequireRole` / `RequireAnyRole` / `RequireAllRoles` authorization decision |
| [`tracing-initialization.mmd`](mermaid/tracing-initialization.mmd) | `setup_tracing` / `_build_span_exporter` — OTLP exporter wiring from `PlatformSettings.otlp_endpoint` |
| [`metrics-flow.mmd`](mermaid/metrics-flow.mmd) | Prometheus HTTP + gRPC metric recording and the `/metrics` exposition endpoint |
| [`grpc-interceptors.mmd`](mermaid/grpc-interceptors.mmd) | `ContextClientInterceptor` and `ContextServerInterceptor` unary-unary call sequence |
| [`observability-stack.mmd`](mermaid/observability-stack.mmd) | How logs, metrics, and traces flow into the local `docker-compose.yml` stack (Loki, Prometheus, Tempo, Grafana) |
| [`consuming-service-wiring.mmd`](mermaid/consuming-service-wiring.mmd) | How a consuming service wires `PlatformSettings` + `create_platform_app` + `RequireRole` + `create_platform_http_client` |
| [`documentation-assets.mmd`](mermaid/documentation-assets.mmd) | Relationship between doc sources (docstrings, ADRs, README, ARCHITECTURE.md) and rendered outputs |
