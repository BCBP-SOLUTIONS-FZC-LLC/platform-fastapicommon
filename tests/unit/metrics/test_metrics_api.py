"""Metric constructors for consuming services (Go ``metrics_api.go``)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

import fastapicommon.metrics.platform as platform
from fastapicommon.metrics import (
    MetricsNotInitializedError,
    MetricsOptions,
    identity_labels,
    init_metrics,
    new_counter,
    new_gauge,
    new_histogram,
    new_shared_counter,
    new_shared_gauge,
    new_shared_histogram,
    register_labeled,
)
from fastapicommon.metrics.compliance import validate
from fastapicommon.metrics.registry import default_registry


@pytest.fixture
def registry() -> Iterator[CollectorRegistry]:
    saved = (
        platform._state.initialized,
        platform._state.registry,
        platform._state.platform,
        platform._state.identity,
    )
    registry = CollectorRegistry()
    init_metrics(
        MetricsOptions(
            service_name="event-consumer", build_version="v1", domain="iam", environment="prod"
        ),
        registry=registry,
    )
    yield registry
    (
        platform._state.initialized,
        platform._state.registry,
        platform._state.platform,
        platform._state.identity,
    ) = saved


def test_constructors_before_initialization_raise() -> None:
    saved = platform._state.identity
    platform._state.identity = None
    try:
        assert identity_labels() == {}
        for build in (
            lambda: new_counter("iam_x_total", "x"),
            lambda: new_shared_counter("platform_retry_total"),
        ):
            with pytest.raises(MetricsNotInitializedError, match="not initialized"):
                build()
    finally:
        platform._state.identity = saved


def test_tier3_and_shared_metrics_carry_the_identity_and_comply(
    registry: CollectorRegistry,
) -> None:
    hits = new_counter("iam_event_consumer_hits_total", "Hits", ["cache"])
    hits.labels(cache="hit").inc()
    depth = new_gauge("iam_event_consumer_backlog", "Backlog")
    depth.set(3)
    lookups = new_histogram("iam_event_consumer_lookup_seconds", "Lookups", ["cache"])
    lookups.labels(cache="miss").observe(0.2)
    custom = new_histogram("iam_event_consumer_custom_seconds", "Custom", buckets=[1.0, 2.0])
    custom.observe(1.5)
    auth = new_shared_counter("iam_auth_events_total")
    auth.labels(event_type="login", outcome="success").inc()
    queue = new_shared_gauge("platform_queue_depth")
    queue.labels(queue="orders").set(5)
    dependency = new_shared_histogram("platform_dependency_request_seconds")
    dependency.labels(dependency="sqs", operation="receive_message", outcome="success").observe(1)

    scrape = generate_latest(registry).decode()
    const = 'domain="iam",environment="prod",service="event-consumer"'
    assert f'iam_event_consumer_hits_total{{cache="hit",{const}}} 1.0' in scrape
    assert f"iam_event_consumer_backlog{{{const}}} 3.0" in scrape
    assert "_created" not in scrape
    assert 'le="0.001"' in scrape  # registry request_duration preset by default
    assert "iam_event_consumer_custom_seconds_bucket{" in scrape and 'le="2.0"' in scrape
    assert identity_labels() == {
        "domain": "iam",
        "service": "event-consumer",
        "environment": "prod",
    }

    report = validate(registry)
    assert report.ok(), str(report)
    assert report.tiers["iam_event_consumer_hits_total"] == "service"
    assert report.tiers["iam_auth_events_total"] == "domain"


def test_constructors_enforce_the_standard(registry: CollectorRegistry) -> None:
    with pytest.raises(ValueError, match="must be named iam_event_consumer_"):
        new_counter("iam_other_hits_total", "x")
    with pytest.raises(ValueError, match="histograms must end in _seconds"):
        new_histogram("iam_event_consumer_size", "x")
    with pytest.raises(ValueError, match="gauges must not end in _total"):
        new_gauge("iam_event_consumer_things_total", "x")
    with pytest.raises(ValueError, match="belongs to domain 'iam'"):
        default_registry().shared_metric("iam_auth_events_total", "counter", "workflow")
    with pytest.raises(ValueError, match="is a gauge, not a counter"):
        new_shared_counter("platform_queue_depth")
    # Already registered by fastapicommon itself.
    with pytest.raises(ValueError, match="Duplicated"):
        new_shared_counter("platform_http_requests_total")


def test_register_labeled_adds_the_identity(registry: CollectorRegistry) -> None:
    class ThirdParty:
        def collect(self) -> Iterator[GaugeMetricFamily]:
            family = GaugeMetricFamily("iam_event_consumer_pool_size", "x", labels=["pool"])
            family.add_metric(["main"], 4)
            yield family

    wrapper = register_labeled(ThirdParty())  # type: ignore[arg-type]
    scrape = generate_latest(registry).decode()
    assert (
        'iam_event_consumer_pool_size{domain="iam",environment="prod",pool="main",'
        'service="event-consumer"} 4.0'
    ) in scrape
    assert wrapper.describe() == []  # ThirdParty has no describe()
    registry.unregister(wrapper)
