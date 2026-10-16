import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from fastapicommon.auth import CurrentUser, get_current_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.bootstrap.app import _tracing_config
from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import NotFoundException

pytestmark = pytest.mark.integration

#: Metrics (on by default) require a registry domain and environment.
IDENTITY: dict[str, object] = {"observability_domain": "iam", "environment": "test"}


def _build_app(**settings_overrides: object) -> object:
    settings = PlatformSettings(_env_file=None, **{**IDENTITY, **settings_overrides})
    app = create_platform_app(settings=settings)

    @app.get("/whoami")
    async def whoami(user: CurrentUser = Depends(get_current_user)) -> dict[str, object]:
        return {"user_id": user.user_id, "tenant_id": user.tenant_id}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise NotFoundException("nope")

    return app


def test_health_and_metrics_wired_by_default() -> None:
    client = TestClient(_build_app())
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/live").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_context_middleware_wired_through_bootstrap() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-user-id": "u1", "x-tenant-id": "t1"})
    assert response.json() == {"user_id": "u1", "tenant_id": "t1"}
    assert response.headers["x-request-id"]


def test_platform_exception_handled_through_bootstrap() -> None:
    client = TestClient(_build_app())
    response = client.get("/boom")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["detail"] == "nope"
    assert body["status"] == 404
    assert body["type"] == "https://errors.company.com/platform/not-found"
    assert {"type", "title", "status", "instance", "traceId", "errorCode", "timestamp"} <= set(body)


def test_metrics_disabled_removes_endpoint() -> None:
    client = TestClient(_build_app(enable_metrics=False), raise_server_exceptions=False)
    response = client.get("/metrics")
    assert response.status_code == 404


def test_app_name_override() -> None:
    settings = PlatformSettings(_env_file=None, **IDENTITY)
    app = create_platform_app(app_name="override-name", settings=settings)
    assert app.title == "override-name"


def test_tracing_config_comes_from_the_settings_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    settings = PlatformSettings(
        _env_file=None, app_name="orders-api", build_version="v1.2.3", **IDENTITY
    )
    config = _tracing_config(settings, "test")
    assert (config.service_name, config.service_version) == ("orders-api", "v1.2.3")
    assert (config.domain, config.environment) == ("iam", "test")
    assert config.endpoint == "localhost:4317"  # gincommon's default
    assert not config.provisional
    override = PlatformSettings(_env_file=None, otlp_endpoint="collector:4317", **IDENTITY)
    assert _tracing_config(override, "test").endpoint == "collector:4317"


def test_create_platform_app_wires_otlp_exporter_without_raising() -> None:
    # Deliberately does not make a request: doing so would create a real span and hand it
    # to the real OTLPSpanExporter's background BatchSpanProcessor thread, which would then
    # spend the rest of the test run retrying a connection to a collector that doesn't
    # exist in this environment, spamming stderr. Request-handling with instrumentation
    # enabled is already covered by every other test in this file (otlp_endpoint unset);
    # this test's only job is confirming construction itself doesn't raise when it's set.
    settings = PlatformSettings(_env_file=None, otlp_endpoint="localhost:4317", **IDENTITY)
    app = create_platform_app(settings=settings)
    assert app is not None


def test_unclassified_exception_not_reraised_to_the_server() -> None:
    # create_platform_app installs UnhandledErrorMiddleware: the default TestClient
    # (raise_server_exceptions=True) would raise if the exception reached the server,
    # which logs its message.
    app = _build_app()

    @app.get("/crash")
    async def crash() -> dict[str, str]:
        raise RuntimeError("pq: secret=hunter2")

    resp = TestClient(app).get("/crash", headers={"x-request-id": "req-1"})
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.headers["x-request-id"] == "req-1"  # still inside ContextMiddleware
    assert "hunter2" not in resp.text


@pytest.mark.parametrize("enable_tracing", [True, False])
def test_on_event_and_on_startup_handlers_still_run(enable_tracing: bool) -> None:
    """Regression: with tracing on, the library passed its own lifespan to FastAPI, which then
    skipped on_startup / on_shutdown and @app.on_event handlers."""
    events: list[str] = []

    async def kwarg_startup() -> None:
        events.append("on_startup")

    settings = PlatformSettings(_env_file=None, enable_tracing=enable_tracing, **IDENTITY)
    with pytest.warns(DeprecationWarning):
        app = create_platform_app(settings=settings, on_startup=[kwarg_startup])

        @app.on_event("startup")
        async def startup() -> None:
            events.append("startup")

        @app.on_event("shutdown")
        async def shutdown() -> None:
            events.append("shutdown")

    with TestClient(app):
        assert events == ["on_startup", "startup"]
    assert events[-1] == "shutdown"


def test_plain_generator_lifespans_are_accepted() -> None:
    """Regression: a lifespan without @asynccontextmanager (FastAPI accepts it) failed with
    'async_generator' object does not support the asynchronous context manager protocol."""
    events: list[str] = []

    async def async_gen_lifespan(app: object):  # type: ignore[no-untyped-def]
        events.append("up")
        yield
        events.append("down")

    app = create_platform_app(
        settings=PlatformSettings(_env_file=None, **IDENTITY), lifespan=async_gen_lifespan
    )
    with TestClient(app):
        assert events == ["up"]
    assert events == ["up", "down"]


def _http_requests(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        dict(r.msg)
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "http_request"
    ]


def test_failure_after_response_started_is_logged_as_aborted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: the access log said status 200 at info for a stream that died mid-body
    (gincommon: re-panic with ErrAbortHandler, logged at debug with "aborted": true)."""
    from fastapi.responses import StreamingResponse

    app = create_platform_app(
        settings=PlatformSettings(_env_file=None, log_level="debug", **IDENTITY)
    )

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body():  # type: ignore[no-untyped-def]
            yield b"partial"
            raise RuntimeError("mid-stream")

        return StreamingResponse(body())

    with caplog.at_level("DEBUG"):
        TestClient(app).get("/stream")
    (entry,) = _http_requests(caplog)
    assert entry["aborted"] is True and entry["status"] == 200 and entry["level"] == "debug"


def test_mounted_route_label_without_metrics(caplog: pytest.LogCaptureFixture) -> None:
    """Regression: with metrics off the label was resolved after Mount rewrote the scope, so
    /admin/users/1 was logged as /users/:uid."""
    from fastapi import FastAPI

    sub = FastAPI()

    @sub.get("/users/{uid}")
    async def user(uid: str) -> dict[str, str]:
        return {"uid": uid}

    app = create_platform_app(
        settings=PlatformSettings(_env_file=None, enable_metrics=False, **IDENTITY)
    )
    app.mount("/admin", sub)
    with caplog.at_level("INFO"):
        assert TestClient(app).get("/admin/users/1").status_code == 200
    (entry,) = _http_requests(caplog)
    assert entry["path"] == "/admin/users/:uid"


@pytest.mark.parametrize(
    ("headers", "detail", "reason"),
    [
        (
            [("x-user-id", "alice"), ("x-user-id", "mallory"), ("x-tenant-id", "t1")],
            "Ambiguous x-user-id header.",
            "ambiguous x-user-id header",
        ),
        (
            [("x-user-id", "alice"), ("x-tenant-id", "t1"), ("x-tenant-id", "t2")],
            "Ambiguous x-tenant-id header.",
            "ambiguous x-tenant-id header",
        ),
        (
            [("x-user-id", "alice"), ("x-tenant-roles", ",".join(f"r{i}" for i in range(65)))],
            "Invalid tenant roles.",
            "invalid tenant roles",
        ),
    ],
)
def test_rejected_identity_is_a_401_with_gincommons_detail(
    headers: list[tuple[str, str]], detail: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (OQ-2 / OQ-3): a repeated identity header was resolved first-value-wins and
    roles had no caps; gincommon answers both with these 401 problems and audit reasons."""
    from fastapicommon.auth import require_authenticated_user

    app = create_platform_app(settings=PlatformSettings(_env_file=None, **IDENTITY))

    @app.get("/me")
    async def me(user: CurrentUser = Depends(require_authenticated_user)) -> dict[str, str]:
        return {"user_id": str(user.user_id)}

    with caplog.at_level("INFO"):
        response = TestClient(app).get("/me", headers=headers)
    assert response.status_code == 401
    body = response.json()
    assert body["errorCode"] == "UNAUTHORIZED" and body["detail"] == detail
    assert "tenantId" not in body
    (entry,) = (
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "auth: request rejected"
    )
    assert entry["reason"] == reason and entry["status"] == 401
