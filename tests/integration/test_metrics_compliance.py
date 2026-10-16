"""Compliance with the Enterprise Platform Observability Standard (Python counterpart of
platform-gincommon's ``test/unit/platformmetrics``).

Drives every instrumented code path through a real ``create_platform_app()`` app and the gRPC
``ContextServerInterceptor``, then checks every emitted ``platform_*`` sample against the
Platform Observability Registry vendored at ``src/fastapicommon/metrics/registry.json`` (a
byte-identical copy of gincommon's ``internal/obsregistry/registry.json``): exactly the registry
labels plus the const labels, every value inside its label vocabulary, and within ``max_values``
— and that nothing but ``platform_*`` (plus prometheus_client's own ``python_*`` / ``process_*``
on the default registry) is emitted: the legacy names were removed without a compatibility
period.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import anyio
import grpc
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from prometheus_client.metrics_core import Metric

from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.grpc import ContextServerInterceptor
from fastapicommon.metrics import MetricsOptions, init_metrics
from fastapicommon.metrics.compliance import Options, validate, validate_text
from fastapicommon.metrics.registry import default_registry

pytestmark = pytest.mark.integration

SETTINGS = {
    "app_name": "event-consumer",
    "build_version": "v1.2.3",
    "observability_domain": "iam",
    "environment": "prod",
    "request_timeout_seconds": 0.05,
    "enable_tracing": False,
}

#: The pre-standard names (status "removed" in the registry); none may ever be emitted.
REMOVED_LEGACY_METRICS = {
    "http_requests_total",
    "http_request_duration_seconds",
    "grpc_requests_total",
    "grpc_request_duration_seconds",
}

#: Every platform metric gincommon emits; fastapicommon has a code path for each of them.
EXPECTED_PLATFORM_METRICS = {
    "platform_build_info",
    "platform_http_requests_total",
    "platform_http_request_duration_seconds",
    "platform_http_request_size_bytes",
    "platform_http_response_size_bytes",
    "platform_http_active_requests",
    "platform_http_panics_total",
    "platform_http_rate_limited_requests_total",
    "platform_http_request_timeouts_total",
    "platform_grpc_server_handled_total",
    "platform_grpc_server_handling_seconds",
    "platform_grpc_server_request_size_bytes",
    "platform_grpc_server_response_size_bytes",
    "platform_grpc_server_inflight_requests",
    "platform_grpc_rate_limited_requests_total",
    "platform_grpc_panics_total",
    "platform_unclassified_errors_total",
}


def _settings(**overrides: Any) -> PlatformSettings:
    return PlatformSettings(_env_file=None, **{**SETTINGS, **overrides})


def _init(**overrides: Any) -> CollectorRegistry:
    settings = _settings(**overrides)
    registry = CollectorRegistry()
    init_metrics(MetricsOptions.from_settings(settings), registry=registry)
    return registry


class _Context:
    def __init__(self, code: grpc.StatusCode | None = None) -> None:
        self._code = code

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()

    def code(self) -> grpc.StatusCode | None:
        return self._code


def _exercise(settings: PlatformSettings) -> TestClient:
    """Drive every instrumented code path once (mirrors gincommon's ``exercise``)."""
    app: FastAPI = create_platform_app(settings=settings)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.post("/items")
    async def rate_limited() -> Response:
        return Response(status_code=429)

    @app.api_route("/items", methods=["PURGE"])
    async def purge() -> Response:
        return Response(status_code=204)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await anyio.sleep(0.5)
        return {}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"chunk"

        return StreamingResponse(body())

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/items/42")
    client.post("/items", content=b"{}")
    client.request("PURGE", "/items")
    client.get("/nope")
    client.get("/boom")
    client.get("/slow")
    client.get("/stream")

    interceptor = ContextServerInterceptor()
    for method, code in (
        ("/iam.v1.Events/Publish", None),
        ("/iam.v1.Events/Publish", grpc.StatusCode.RESOURCE_EXHAUSTED),
        ("/iam.v1.Events/Publish", grpc.StatusCode.INTERNAL),
        ("", None),
    ):
        handler = grpc.unary_unary_rpc_method_handler(lambda request, context: b"ok")
        wrapped = interceptor.intercept_service(
            lambda _, h=handler: h, SimpleNamespace(method=method)
        )
        wrapped.unary_unary(b"req", _Context(code))

    def explode(request: Any, context: Any) -> Any:
        raise RuntimeError("boom")

    handler = grpc.unary_unary_rpc_method_handler(explode)
    wrapped = interceptor.intercept_service(
        lambda _: handler, SimpleNamespace(method="/iam.v1.Events/Publish")
    )
    with pytest.raises(RuntimeError):
        wrapped.unary_unary(b"req", _Context())
    return client


def _families(registry: CollectorRegistry) -> Iterator[tuple[str, Metric]]:
    """(registry metric name, family) for every collected family."""
    for family in registry.collect():
        # prometheus_client strips a counter's _total suffix from the family name.
        name = family.name + "_total" if family.type == "counter" else family.name
        yield name, family


def _check_compliance(registry: CollectorRegistry) -> set[str]:
    """Assert the standard's label rules on every platform_* sample; returns the names seen."""
    vocabulary = default_registry()
    const = set(vocabulary.const_labels)
    seen: set[str] = set()
    distinct: dict[tuple[str, str], set[str]] = defaultdict(set)
    for name, family in _families(registry):
        if not name.startswith("platform_"):
            continue
        spec = vocabulary.lookup(name)
        assert spec is not None, f"{name} is not in the registry"
        assert spec.tier == "platform"
        assert family.type == spec.type, name
        assert family.documentation == spec.help, name
        seen.add(name)
        for sample in family.samples:
            assert not sample.name.endswith("_created"), sample.name
            labels = dict(sample.labels)
            if sample.name.endswith("_bucket"):
                labels.pop("le")
            assert set(labels) == const | set(spec.labels), (sample.name, sorted(labels))
            for label, value in labels.items():
                assert vocabulary.labels[label].allows(value), (sample.name, label, value)
                distinct[(name, label)].add(value)
    for (name, label), values in distinct.items():
        max_values = vocabulary.labels[label].max_values
        assert max_values is None or len(values) <= max_values, (name, label)
    return seen


def test_platform_metrics_comply_with_the_registry() -> None:
    registry = _init()
    client = _exercise(_settings())

    assert _check_compliance(registry) == EXPECTED_PLATFORM_METRICS

    # Histograms use the registry's bucket presets (identical in every service).
    duration_buckets = sorted(
        {
            float(sample.labels["le"])
            for name, family in _families(registry)
            if name == "platform_http_request_duration_seconds"
            for sample in family.samples
            if sample.name.endswith("_bucket") and sample.labels["le"] != "+Inf"
        }
    )
    assert tuple(duration_buckets) == default_registry().buckets("request_duration")

    const = {"domain": "iam", "environment": "prod", "service": "event-consumer"}
    publish = {"grpc_method": "/iam.v1.Events/Publish"}
    ok = {"status_class": "2xx", "error_class": "success"}
    for name, labels, value in (
        ("platform_http_requests_total", {"method": "OTHER", "route": "/items", **ok}, 1),
        (
            "platform_http_requests_total",
            {"method": "GET", "route": "/items/:item_id", **ok},
            1,
        ),
        (
            "platform_http_requests_total",
            {"method": "GET", "route": "unknown", "status_class": "4xx"}
            | {"error_class": "client_error"},
            1,
        ),
        ("platform_http_panics_total", {"method": "GET", "route": "/boom"}, 1),
        ("platform_http_rate_limited_requests_total", {"method": "POST", "route": "/items"}, 1),
        ("platform_http_request_timeouts_total", {"method": "GET", "route": "/slow"}, 1),
        ("platform_http_active_requests", {"route": "/items/:item_id"}, 0),
        ("platform_http_response_size_bytes_sum", {"method": "GET", "route": "/stream", **ok}, 5),
        ("platform_grpc_panics_total", publish, 1),
        ("platform_grpc_rate_limited_requests_total", publish, 1),
        (
            "platform_grpc_server_handled_total",
            {"grpc_method": "unknown", "grpc_code": "OK", **ok},
            1,
        ),
        ("platform_grpc_server_inflight_requests", publish, 0),
        ("platform_build_info", {"version": "v1.2.3"}, 1),
    ):
        assert registry.get_sample_value(name, {**const, **labels}) == value, (name, labels)

    scrape = client.get("/metrics").text
    assert "platform_http_requests_total{" in scrape
    assert "_created{domain=" not in scrape
    _assert_no_legacy_families(scrape)


def _assert_no_legacy_families(scrape: str) -> None:
    for line in scrape.splitlines():
        # "# HELP|TYPE <name> ..." or "<name>{labels} value" / "<name> value".
        name = line.split()[2] if line.startswith("# ") else line.split("{")[0].split(" ")[0]
        assert name not in REMOVED_LEGACY_METRICS, line
        family = name.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count")
        assert family not in REMOVED_LEGACY_METRICS, line


def test_scrape_passes_the_full_standard() -> None:
    """The rule set consuming services run (Go ``obsregistry.Validate``): 0 errors; the only
    warnings are the registry's ``proposed`` statuses, which ``allow_proposed_owners`` exempts."""
    registry = _init()
    client = _exercise(_settings())
    report = validate(registry)
    assert report.ok(), str(report)
    assert {f.rule for f in report.warnings()} == {"status"}
    assert set(report.tiers) == EXPECTED_PLATFORM_METRICS
    strict = Options(strict=True, allow_proposed_owners=("platform-gincommon",))
    assert validate(registry, strict).ok()
    # The served /metrics page itself (prometheus_client's default registry included).
    assert validate_text(client.get("/metrics").text, strict).ok()


def test_only_platform_metrics_are_emitted() -> None:
    registry = _init()
    _exercise(_settings())
    names = {name for name, _ in _families(registry)}
    assert names == EXPECTED_PLATFORM_METRICS
    assert not names & REMOVED_LEGACY_METRICS


def test_default_registry_scrape_has_only_platform_and_runtime_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OBSERVABILITY_DOMAIN", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    client = _exercise(_settings())  # create_platform_app() on prometheus_client's REGISTRY
    scrape = client.get("/metrics").text
    _assert_no_legacy_families(scrape)
    families = {line.split()[2] for line in scrape.splitlines() if line.startswith("# TYPE ")}
    assert families >= EXPECTED_PLATFORM_METRICS
    assert all(name.startswith(("platform_", "python_", "process_")) for name in families)


def test_missing_domain_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSERVABILITY_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="OBSERVABILITY_DOMAIN"):
        create_platform_app(settings=_settings(observability_domain=""))
    with pytest.raises(ValueError, match="OBSERVABILITY_DOMAIN"):
        ContextServerInterceptor(_settings(observability_domain=""))
