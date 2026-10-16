import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

from fastapicommon.auth import CurrentUser, get_current_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.bootstrap.app import _build_span_exporter
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


def test_build_span_exporter_none_when_no_otlp_endpoint() -> None:
    settings = PlatformSettings(_env_file=None)
    assert _build_span_exporter(settings) is None


def test_build_span_exporter_builds_otlp_exporter_when_configured() -> None:
    settings = PlatformSettings(_env_file=None, otlp_endpoint="localhost:4317")
    exporter = _build_span_exporter(settings)
    assert isinstance(exporter, OTLPSpanExporter)


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
