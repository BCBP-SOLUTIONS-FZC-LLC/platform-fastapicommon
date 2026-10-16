from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anyio
import grpc
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, CollectorRegistry
from starlette.applications import Starlette
from starlette.routing import Mount

from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.grpc.server import ContextServerInterceptor, _platform_grpc_code
from fastapicommon.metrics import (
    MetricsOptions,
    PrometheusMiddleware,
    init_metrics,
    init_metrics_from_settings,
    metrics_identity,
    metrics_registry,
    metrics_router,
    platform_metrics,
    platform_metrics_enabled,
    resolve_metrics_options,
)
from fastapicommon.metrics.platform import (
    grpc_code_label,
    grpc_error_class,
    grpc_status_class,
    http_error_class,
    http_method_label,
    http_route_label,
    http_status_class,
    proto_message_size,
    request_body_size,
    route_template_label,
)
from fastapicommon.middleware import TimeoutMiddleware

IAM = MetricsOptions(
    service_name="event-consumer", build_version="v1.2.3", domain="iam", environment="prod"
)
CONST = {"domain": "iam", "environment": "prod", "service": "event-consumer"}


def _clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OBSERVABILITY_DOMAIN", "OBSERVABILITY_ENVIRONMENT", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)


def _value(registry: CollectorRegistry, name: str, **labels: str) -> float | None:
    result = registry.get_sample_value(name, {**CONST, **labels})
    return None if result is None else float(result)


# ------------------------------------------------------------------- options / identity


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (MetricsOptions("svc", environment="prod"), "OBSERVABILITY_DOMAIN"),
        (MetricsOptions("svc"), "a domain is required"),
        (MetricsOptions("svc", domain="hr", environment="prod"), "domain 'hr'"),
        (MetricsOptions("Bad Name", domain="iam", environment="prod"), "service 'Bad Name'"),
        (MetricsOptions("svc", domain="iam", environment="qa"), "environment 'qa'"),
        (MetricsOptions("svc", domain="iam", environment="production"), "environment"),
        (MetricsOptions("svc", domain="iam"), "environment ''"),
    ],
)
def test_resolve_rejects_invalid_identity(
    monkeypatch: pytest.MonkeyPatch, options: MetricsOptions, message: str
) -> None:
    _clear_identity_env(monkeypatch)
    with pytest.raises(ValueError, match=message):
        resolve_metrics_options(options)
    with pytest.raises(ValueError, match=message):
        init_metrics(options, registry=CollectorRegistry())


def test_resolve_defaults_version_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    identity = resolve_metrics_options(MetricsOptions("svc", domain="iam", environment="dev"))
    assert identity.version == "unknown"


def test_init_without_domain_raises_and_initializes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(ValueError, match="OBSERVABILITY_DOMAIN"):
        init_metrics(MetricsOptions("svc"))
    assert not platform_metrics_enabled()
    assert metrics_identity() is None


def test_resolve_env_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv("OBSERVABILITY_DOMAIN", "billing")
    monkeypatch.setenv("ENVIRONMENT", "test")
    identity = resolve_metrics_options(MetricsOptions("svc"))
    assert (identity.domain, identity.environment) == ("billing", "test")

    monkeypatch.setenv("APP_ENV", " Staging ")
    assert resolve_metrics_options(MetricsOptions("svc")).environment == "staging"

    # Explicit options win over the environment.
    explicit = resolve_metrics_options(MetricsOptions("svc", domain="iam", environment="local"))
    assert (explicit.domain, explicit.environment) == ("iam", "local")

    # OBSERVABILITY_ENVIRONMENT was removed from the standard: it alone is not an environment.
    monkeypatch.delenv("APP_ENV")
    monkeypatch.delenv("ENVIRONMENT")
    monkeypatch.setenv("OBSERVABILITY_ENVIRONMENT", "prod")
    with pytest.raises(ValueError, match="environment ''"):
        resolve_metrics_options(MetricsOptions("svc"))


def test_options_from_settings_only_count_an_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_identity_env(monkeypatch)
    defaulted = MetricsOptions.from_settings(
        PlatformSettings(_env_file=None, observability_domain="iam")
    )
    assert defaulted.environment == ""
    assert defaulted.domain == "iam"

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("BUILD_VERSION", "1.0.0")
    monkeypatch.setenv("OBSERVABILITY_DOMAIN", "billing")
    from_env = MetricsOptions.from_settings(PlatformSettings(_env_file=None, app_name="svc"))
    assert from_env == MetricsOptions("svc", "1.0.0", "billing", "dev")


# ---------------------------------------------------------------------------- label helpers


def test_http_label_helpers_mirror_go() -> None:
    assert http_method_label("GET") == "GET"
    assert http_method_label("TRACE") == "TRACE"
    assert http_method_label("PURGE") == "OTHER"
    assert http_method_label("get") == "OTHER"
    assert [http_status_class(c) for c in (101, 200, 302, 404, 500)] == [
        "5xx",
        "2xx",
        "2xx",
        "4xx",
        "5xx",
    ]
    assert [http_error_class(c) for c in (101, 204, 399, 429, 503)] == [
        "server_error",
        "success",
        "success",
        "client_error",
        "server_error",
    ]


def test_grpc_label_helpers_mirror_go() -> None:
    assert grpc_code_label("CANCELLED") == "Canceled"
    assert grpc_code_label("RESOURCE_EXHAUSTED") == "ResourceExhausted"
    assert grpc_code_label("BOGUS") == "Unknown"
    assert all(
        grpc_code_label(code.name) != "Unknown" or code.name == "UNKNOWN"
        for code in grpc.StatusCode
    )
    assert (grpc_status_class("OK"), grpc_error_class("OK")) == ("2xx", "success")
    assert (grpc_status_class("DeadlineExceeded"), grpc_error_class("NotFound")) == (
        "4xx",
        "client_error",
    )
    assert (grpc_status_class("Internal"), grpc_error_class("Unknown")) == ("5xx", "server_error")


def test_route_template_label_uses_registry_syntax() -> None:
    assert route_template_label("/items/{item_id}") == "/items/:item_id"
    assert route_template_label("/items/{item_id:int}/x") == "/items/:item_id/x"
    assert route_template_label("/files/{rest:path}") == "/files/*rest"
    assert route_template_label("/health") == "/health"


def test_http_route_label_resolves_mounts_and_unknown() -> None:
    sub = FastAPI()

    @sub.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {}

    app = FastAPI()
    app.mount("/v2", sub)
    app.mount("/static", Starlette())

    def scope(path: str, method: str = "GET") -> dict[str, Any]:
        return {"type": "http", "path": path, "method": method, "root_path": "", "app": app}

    assert http_route_label(scope("/v2/items/1")) == "/v2/items/:item_id"
    assert http_route_label(scope("/v2/nope")) == "unknown"
    assert http_route_label(scope("/static/app.js")) == "/static"
    assert http_route_label(scope("/nope")) == "unknown"
    assert http_route_label({"type": "http", "path": "/"}) == "unknown"

    root = FastAPI()
    root.router.routes.append(Mount("", app=Starlette()))
    assert http_route_label({**scope("/x"), "app": root}) == "unknown"

    class _NoPath:
        def matches(self, scope: Any) -> tuple[Any, dict[str, Any]]:
            from starlette.routing import Match

            return Match.FULL, {}

    odd = SimpleNamespace(router=SimpleNamespace(routes=[_NoPath()]))
    assert http_route_label({**scope("/x"), "app": odd}) == "unknown"


def test_size_helpers() -> None:
    assert request_body_size(None) == 0
    assert request_body_size("12") == 12
    assert request_body_size("-1") == 0
    assert request_body_size("abc") == 0
    assert proto_message_size(None) == 0
    assert proto_message_size(b"raw bytes") == 0
    assert proto_message_size(SimpleNamespace(ByteSize=lambda: 7)) == 7


# --------------------------------------------------------------------------- initialization


def test_init_first_call_wins_and_custom_registry_bypasses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_identity_env(monkeypatch)
    first = MetricsOptions("first-svc", domain="workflow", environment="dev")
    init_metrics(first)
    assert platform_metrics_enabled()
    assert metrics_registry() is REGISTRY

    # Already initialized: only validates.
    init_metrics(IAM)
    identity = metrics_identity()
    assert identity is not None
    assert identity.service == "first-svc"
    with pytest.raises(ValueError):
        init_metrics(MetricsOptions("svc", domain="hr", environment="prod"))
    with pytest.raises(ValueError, match="OBSERVABILITY_DOMAIN"):
        init_metrics(MetricsOptions("svc", environment="prod"))

    registry = CollectorRegistry()
    init_metrics(IAM, registry=registry)
    assert platform_metrics_enabled()
    assert metrics_registry() is registry
    identity = metrics_identity()
    assert identity is not None
    assert (identity.domain, identity.service, identity.environment, identity.version) == (
        "iam",
        "event-consumer",
        "prod",
        "v1.2.3",
    )
    # Only the platform collector is registered on the custom registry.
    platform = platform_metrics()
    assert platform is not None
    assert set(registry._collector_to_names) == {platform.collector}
    assert _value(registry, "platform_build_info", version="v1.2.3") == 1


def test_default_registry_has_no_legacy_families(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    init_metrics(MetricsOptions("svc-a", domain="iam", environment="dev"))
    names = {family.name for family in REGISTRY.collect()}
    assert "platform_build_info" in names
    for legacy in ("http_requests", "http_request_duration_seconds", "grpc_requests"):
        assert legacy not in names
    assert "grpc_request_duration_seconds" not in names


def test_init_from_settings_custom_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    registry = CollectorRegistry()
    settings = PlatformSettings(
        _env_file=None,
        app_name="svc",
        environment="test",
        observability_domain="workflow",
    )
    init_metrics_from_settings(settings, registry=registry)
    assert (
        registry.get_sample_value(
            "platform_build_info",
            {"domain": "workflow", "environment": "test", "service": "svc", "version": "unknown"},
        )
        == 1
    )


# ----------------------------------------------------------------------------- HTTP middleware


def _metrics_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TimeoutMiddleware, timeout_seconds=0.05)
    app.add_middleware(PrometheusMiddleware)
    app.include_router(metrics_router)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"abc"
            yield b"de"

        return StreamingResponse(body())

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("boom")

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await anyio.sleep(0.5)
        return {}

    return app


def test_platform_http_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    registry = CollectorRegistry()
    init_metrics(IAM, registry=registry)
    client = TestClient(_metrics_app(), raise_server_exceptions=False)

    assert client.get("/items/1").status_code == 200
    assert client.get("/stream").text == "abcde"
    assert client.get("/crash").status_code == 500
    assert client.get("/slow").status_code == 503

    ok = {"method": "GET", "status_class": "2xx", "error_class": "success"}
    assert _value(registry, "platform_http_requests_total", route="/items/:item_id", **ok) == 1
    assert _value(registry, "platform_http_response_size_bytes_sum", route="/stream", **ok) == 5
    server_error = {"method": "GET", "status_class": "5xx", "error_class": "server_error"}
    assert _value(registry, "platform_http_requests_total", route="/crash", **server_error) == 1
    assert _value(registry, "platform_http_panics_total", method="GET", route="/crash") == 1
    assert _value(registry, "platform_http_requests_total", route="/slow", **server_error) == 1
    assert (
        _value(registry, "platform_http_request_timeouts_total", method="GET", route="/slow") == 1
    )
    assert _value(registry, "platform_http_active_requests", route="/crash") == 0

    scrape = client.get("/metrics").text
    assert "platform_http_requests_total{" in scrape
    assert "platform_http_requests_created" not in scrape
    assert not any(line.startswith("http_request") for line in scrape.splitlines())


def test_streaming_response_without_body_iterator_observes_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.responses import Response

    from fastapicommon.metrics.middleware import _content_length, _count_body

    observed: list[float] = []
    response = Response(status_code=204)
    del response.headers["content-length"]
    assert _content_length(response) is None
    _count_body(response, observed.append)
    assert observed == [0.0]

    response.headers["content-length"] = "nan"
    assert _content_length(response) is None


# --------------------------------------------------------------------------------------- gRPC


class _Context:
    def __init__(self, code: grpc.StatusCode | None = None) -> None:
        self._code = code

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()

    def code(self) -> grpc.StatusCode | None:
        return self._code


def _call(interceptor: ContextServerInterceptor, method: str, behavior: Any, context: Any) -> Any:
    handler = grpc.unary_unary_rpc_method_handler(behavior)
    wrapped = interceptor.intercept_service(lambda _: handler, SimpleNamespace(method=method))
    return wrapped.unary_unary(SimpleNamespace(ByteSize=lambda: 3), context)


def test_platform_grpc_code_resolution() -> None:
    assert _platform_grpc_code(object(), raised=False) == ("OK", False)
    assert _platform_grpc_code(object(), raised=True) == ("Unknown", True)
    assert _platform_grpc_code(_Context(grpc.StatusCode.OK), raised=True) == ("Unknown", True)
    assert _platform_grpc_code(_Context(grpc.StatusCode.NOT_FOUND), raised=False) == (
        "NotFound",
        False,
    )
    assert _platform_grpc_code(_Context(grpc.StatusCode.INTERNAL), raised=True) == (
        "Internal",
        False,
    )


def test_platform_grpc_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    registry = CollectorRegistry()
    init_metrics(IAM, registry=registry)
    interceptor = ContextServerInterceptor()
    method = "/iam.v1.Events/Publish"

    assert _call(interceptor, method, lambda r, c: SimpleNamespace(ByteSize=lambda: 5), _Context())
    _call(interceptor, method, lambda r, c: None, _Context(grpc.StatusCode.RESOURCE_EXHAUSTED))

    def explode(request: Any, context: Any) -> Any:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _call(interceptor, method, explode, _Context())

    labels = {"grpc_method": method}
    ok = {**labels, "status_class": "2xx", "error_class": "success"}
    assert _value(registry, "platform_grpc_server_handled_total", grpc_code="OK", **ok) == 1
    assert _value(registry, "platform_grpc_server_request_size_bytes_sum", **ok) == 3
    assert _value(registry, "platform_grpc_server_response_size_bytes_sum", **ok) == 5
    assert _value(registry, "platform_grpc_rate_limited_requests_total", **labels) == 1
    assert _value(registry, "platform_grpc_panics_total", **labels) == 1
    unknown = {**labels, "grpc_code": "Unknown", "status_class": "5xx"}
    assert (
        _value(
            registry, "platform_grpc_server_handled_total", error_class="server_error", **unknown
        )
        == 1
    )
    assert _value(registry, "platform_grpc_server_inflight_requests", **labels) == 0


def test_context_server_interceptor_initializes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_identity_env(monkeypatch)
    with pytest.raises(ValueError, match="domain"):
        ContextServerInterceptor(
            PlatformSettings(_env_file=None, observability_domain="hr", environment="prod")
        )
    ContextServerInterceptor(
        PlatformSettings(
            _env_file=None, app_name="grpc-svc", observability_domain="iam", environment="prod"
        )
    )
    assert platform_metrics_enabled()


# ---------------------------------------------------------------------------------- bootstrap


@pytest.mark.parametrize("enable_metrics", [True, False])
def test_create_platform_app_raises_on_invalid_identity(
    monkeypatch: pytest.MonkeyPatch, enable_metrics: bool
) -> None:
    _clear_identity_env(monkeypatch)
    with pytest.raises(ValueError, match="environment ''"):
        create_platform_app(
            settings=PlatformSettings(
                _env_file=None, observability_domain="iam", enable_metrics=enable_metrics
            )
        )
    monkeypatch.setenv("OBSERVABILITY_DOMAIN", "hr")
    with pytest.raises(ValueError, match="domain 'hr'"):
        create_platform_app(
            settings=PlatformSettings(
                _env_file=None, environment="prod", enable_metrics=enable_metrics
            )
        )


def test_create_platform_app_requires_a_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    with pytest.raises(ValueError, match="OBSERVABILITY_DOMAIN"):
        create_platform_app(settings=PlatformSettings(_env_file=None, environment="prod"))
    with pytest.raises(ValueError, match="OBSERVABILITY_DOMAIN"):
        ContextServerInterceptor(PlatformSettings(_env_file=None, environment="prod"))
    assert not platform_metrics_enabled()


def test_metrics_disabled_needs_no_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_identity_env(monkeypatch)
    settings = PlatformSettings(_env_file=None, enable_metrics=False)
    app = create_platform_app(settings=settings)
    ContextServerInterceptor(settings)
    assert not platform_metrics_enabled()
    assert TestClient(app).get("/metrics").status_code == 404


def test_platform_metrics_require_their_registry_entries() -> None:
    import json

    from fastapicommon.metrics import PlatformMetrics
    from fastapicommon.metrics.registry import load_registry, registry_bytes

    document = json.loads(registry_bytes())
    document["metrics"] = [
        m for m in document["metrics"] if m["name"] != "platform_grpc_panics_total"
    ]
    identity = resolve_metrics_options(IAM)
    with pytest.raises(RuntimeError, match="platform_grpc_panics_total"):
        PlatformMetrics(identity, load_registry(json.dumps(document)))

    for entry in document["metrics"]:
        if entry["name"] == "platform_http_request_size_bytes":
            del entry["buckets"]
    document["metrics"].append(
        {"name": "platform_grpc_panics_total", "tier": "platform", "type": "counter", "help": "h"}
    )
    PlatformMetrics(identity, load_registry(json.dumps(document)))


def test_route_label_resolves_included_routers() -> None:
    """FastAPI keeps included routers as lazy entries (no ``path``); their routes must still get
    their full, prefixed template, not ``unknown`` (they did not on FastAPI 0.140)."""
    from fastapi import APIRouter, FastAPI

    from fastapicommon.metrics.platform import http_route_label

    app = FastAPI()
    outer, inner = APIRouter(prefix="/widgets"), APIRouter(prefix="/v1")

    @inner.get("/{widget_id}")
    async def widget(widget_id: str) -> dict[str, str]:
        return {}

    @outer.get("/me")
    async def me() -> dict[str, str]:
        return {}

    outer.include_router(inner)
    app.include_router(outer)

    def label(path: str) -> str:
        return http_route_label(
            {
                "type": "http",
                "path": path,
                "method": "GET",
                "app": app,
                "root_path": "",
                "headers": [],
                "query_string": b"",
            }
        )

    assert label("/widgets/me") == "/widgets/me"
    assert label("/widgets/v1/42") == "/widgets/v1/:widget_id"
    assert label("/widgets/v2/42") == "unknown"


def test_route_table_matched_once_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracing, logging and metrics all need the route label; it is resolved once and kept in
    the scope instead of re-running the route table in each middleware."""
    from fastapicommon.metrics import platform as platform_module

    calls = 0
    real = platform_module._match_routes

    def counting(routes: Any, scope: Any, prefix: str) -> str | None:
        nonlocal calls
        if prefix == "":
            calls += 1
        return real(routes, scope, prefix)

    monkeypatch.setattr(platform_module, "_match_routes", counting)
    app = create_platform_app(
        settings=PlatformSettings(_env_file=None, observability_domain="iam", environment="test")
    )

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    assert TestClient(app).get("/items/1").status_code == 200
    assert calls == 1
