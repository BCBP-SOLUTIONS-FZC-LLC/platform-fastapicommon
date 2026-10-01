from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

import fastapicommon.metrics as metrics
from fastapicommon.metrics import MetricsOptions, PrometheusMiddleware, init_metrics, metrics_router

#: The pre-standard names removed in favour of the platform_* metrics (never deployed).
REMOVED_LEGACY_METRICS = (
    "http_requests_total",
    "http_request_duration_seconds",
    "grpc_requests_total",
    "grpc_request_duration_seconds",
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)
    app.include_router(metrics_router)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    return app


def _assert_no_legacy_families(scrape: str) -> None:
    for name in REMOVED_LEGACY_METRICS:
        assert f"# TYPE {name} " not in scrape
        assert not any(line.startswith(name) for line in scrape.splitlines())


def test_middleware_is_a_pass_through_before_init() -> None:
    client = TestClient(_build_app())
    assert client.get("/items/123").json() == {"item_id": "123"}
    scrape = client.get("/metrics").text
    assert "platform_http_requests_total" not in scrape
    _assert_no_legacy_families(scrape)


def test_metrics_endpoint_exposes_only_platform_http_metrics() -> None:
    init_metrics(
        MetricsOptions("svc", domain="iam", environment="test"), registry=CollectorRegistry()
    )
    client = TestClient(_build_app())
    client.get("/items/abc")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert 'platform_http_requests_total{domain="iam"' in response.text
    _assert_no_legacy_families(response.text)


def test_legacy_metric_api_is_removed() -> None:
    for attr in (
        "HTTP_REQUESTS_TOTAL",
        "HTTP_REQUEST_DURATION_SECONDS",
        "GRPC_REQUESTS_TOTAL",
        "GRPC_REQUEST_DURATION_SECONDS",
        "record_grpc_request",
    ):
        assert not hasattr(metrics, attr)
        assert attr not in metrics.__all__
