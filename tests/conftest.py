from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest
from prometheus_client import REGISTRY

from fastapicommon.metrics import platform
from fastapicommon.telemetry import shutdown_tracing


@pytest.fixture(autouse=True)
def _isolate_metrics_state() -> Iterator[None]:
    """Metrics initialization is process-wide and first-call-wins (like Go's sync.Once), so
    restore it after every test: a test that initializes metrics must not leak that identity —
    or its collectors on prometheus_client's default REGISTRY — into the next test."""
    saved = dataclasses.replace(platform._state)
    before = set(REGISTRY._collector_to_names)
    yield
    for collector in set(REGISTRY._collector_to_names) - before:
        if isinstance(collector, platform._WithoutCreatedSeries):
            REGISTRY.unregister(collector)
    for field in dataclasses.fields(saved):
        setattr(platform._state, field.name, getattr(saved, field.name))


@pytest.fixture(autouse=True)
def _isolate_tracing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tracing defaults to exporting to localhost:4317 (as in gincommon); keep spans in-process
    unless a test opts in, and stop the provider a test installed."""
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    yield
    shutdown_tracing()
