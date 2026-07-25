from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapicommon.metrics import (
    GRPC_REQUEST_DURATION_SECONDS,
    GRPC_REQUESTS_TOTAL,
    HTTP_REQUESTS_TOTAL,
    PrometheusMiddleware,
    metrics_router,
    record_grpc_request,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)
    app.include_router(metrics_router)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    return app


def test_http_metrics_recorded_with_route_template() -> None:
    client = TestClient(_build_app())
    client.get("/items/123")

    sample_value = HTTP_REQUESTS_TOTAL.labels(
        method="GET", path="/items/{item_id}", status_code="200"
    )._value.get()
    assert sample_value >= 1


def test_metrics_endpoint_exposes_prometheus_format() -> None:
    client = TestClient(_build_app())
    client.get("/items/abc")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text


def test_record_grpc_request_updates_counters() -> None:
    before = GRPC_REQUESTS_TOTAL.labels(method="/svc/Method", status_code="OK")._value.get()
    record_grpc_request(method="/svc/Method", status_code="OK", duration_seconds=0.01)
    after = GRPC_REQUESTS_TOTAL.labels(method="/svc/Method", status_code="OK")._value.get()
    assert after == before + 1

    sample = GRPC_REQUEST_DURATION_SECONDS.labels(method="/svc/Method")
    assert sample._sum.get() >= 0.01
