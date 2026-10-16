from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import grpc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from fastapicommon.exceptions import (
    ConflictException,
    PlatformException,
    UnhandledErrorMiddleware,
    register_exception_handlers,
)
from fastapicommon.grpc.server import ContextServerInterceptor
from fastapicommon.metrics import MetricsOptions, init_metrics
from fastapicommon.middleware import ContextMiddleware

IAM = MetricsOptions(service_name="orders", domain="iam", environment="test")


@pytest.fixture
def registry() -> Iterator[CollectorRegistry]:
    # conftest's autouse fixture restores the process-wide metrics state afterwards.
    reg = CollectorRegistry()
    init_metrics(IAM, registry=reg)
    yield reg


def _value(reg: CollectorRegistry, transport: str) -> float | None:
    return reg.get_sample_value(
        "platform_unclassified_errors_total",
        {"domain": "iam", "service": "orders", "environment": "test", "transport": transport},
    )


def test_both_series_start_at_zero(registry: CollectorRegistry) -> None:
    assert _value(registry, "http") == 0.0
    assert _value(registry, "grpc") == 0.0


def test_http_unclassified_counted_classified_not(registry: CollectorRegistry) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(ContextMiddleware)

    @app.get("/plain")
    async def plain() -> None:
        raise RuntimeError("secret")

    @app.get("/classified")
    async def classified() -> None:
        raise ConflictException("Taken.")

    @app.get("/deliberate")
    async def deliberate() -> None:
        raise PlatformException()

    client = TestClient(app)
    client.get("/plain")
    client.get("/plain")
    client.get("/classified")
    client.get("/deliberate")
    assert _value(registry, "http") == 2.0
    assert _value(registry, "grpc") == 0.0


def test_grpc_unclassified_counted(registry: CollectorRegistry) -> None:
    def explode(request: bytes, context: object) -> bytes:
        raise RuntimeError("secret")

    handler = grpc.unary_unary_rpc_method_handler(explode)
    wrapped = ContextServerInterceptor().intercept_service(
        lambda _: handler, SimpleNamespace(method="/t.S/M")
    )

    class Ctx:
        def invocation_metadata(self) -> tuple[()]:
            return ()

        def code(self) -> None:
            return None

    with pytest.raises(RuntimeError):
        wrapped.unary_unary(b"x", Ctx())
    assert _value(registry, "grpc") == 1.0
