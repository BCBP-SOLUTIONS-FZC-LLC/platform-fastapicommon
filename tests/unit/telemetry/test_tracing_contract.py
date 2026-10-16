"""The tracing core against gincommon's ``otel`` package: configuration from ``OTEL_*``, resource,
sampler, provider lifecycle (TRC-1 … TRC-4) and the tracing helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased

from fastapicommon.logging import configure_logging
from fastapicommon.telemetry import (
    UNKNOWN_SERVICE_NAME,
    TracingConfig,
    active_tracing_config,
    ensure_tracing,
    extract_trace_context,
    flush_tracing,
    init_tracing,
    init_tracing_from_env,
    inject_trace_context,
    new_tracer,
    noop_tracer,
    record_span_error,
    setup_tracing,
    shutdown_tracing,
    span_trace_id,
)
from fastapicommon.telemetry import tracing as tracing_module

OTEL_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_INSECURE",
    "OTEL_TRACES_SAMPLER_RATIO",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_PROPAGATOR_BAGGAGE",
    "OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED",
    "OTEL_SERVICE_NAME",
    "APP_NAME",
    "APP_ENV",
    "ENVIRONMENT",
    "BUILD_VERSION",
    "OBSERVABILITY_DOMAIN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in OTEL_VARS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_from_env_production_defaults() -> None:
    config = TracingConfig.from_env(service_name="orders-api")
    assert config.endpoint == "localhost:4317" and not config.insecure
    assert config.sample_ratio == 0.1 and not config.enable_baggage
    assert config.service_version == "unknown" and not config.provisional


def test_from_env_dev_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "Dev")
    monkeypatch.setenv("OBSERVABILITY_DOMAIN", " IAM ")
    monkeypatch.setenv("BUILD_VERSION", "v9")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")
    monkeypatch.setenv("OTEL_PROPAGATOR_BAGGAGE", "yes")
    monkeypatch.setenv("OTEL_TRACES_IGNORE_REMOTE_PARENT_SAMPLED", "1")
    config = TracingConfig.from_env(service_name="svc")
    assert (config.insecure, config.sample_ratio) == (True, 1.0)
    assert (config.domain, config.environment, config.service_version) == ("iam", "dev", "v9")
    assert config.endpoint == "collector:4317"
    assert config.enable_baggage and config.ignore_remote_parent_sampled


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "none"}, ""),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "OFF"}, ""),
        ({"OTEL_TRACES_EXPORTER": "none"}, ""),
        ({"OTEL_TRACES_EXPORTER": "otlp"}, "localhost:4317"),
    ],
)
def test_export_can_be_disabled(
    env: dict[str, str], value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    assert TracingConfig.from_env(service_name="svc").endpoint == value
    # OTEL_TRACES_EXPORTER=none wins over an explicit endpoint (OTLP_ENDPOINT) too.
    explicit = TracingConfig.from_env(service_name="svc", endpoint="explicit:1").endpoint
    assert explicit == ("" if env.get("OTEL_TRACES_EXPORTER") == "none" else "explicit:1")


@pytest.mark.parametrize("value", ["none", "Off", " disabled "])
def test_explicit_endpoint_can_disable_export(value: str) -> None:
    """Regression: OTLP_ENDPOINT=none used to build an exporter for the host 'none'."""
    assert TracingConfig.from_env(service_name="svc", endpoint=value).endpoint == ""


def test_explicit_environment_wins_for_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: APP_ENV decided the dev defaults even when an environment was passed."""
    monkeypatch.setenv("APP_ENV", "prod")
    local = TracingConfig.from_env(service_name="svc", environment="local")
    assert local.environment == "local" and local.insecure and local.sample_ratio == 1.0
    prod = TracingConfig.from_env(service_name="svc")
    assert prod.environment == "prod" and not prod.insecure and prod.sample_ratio == 0.1


@pytest.mark.parametrize(
    ("env", "ratio"),
    [
        ({"OTEL_TRACES_SAMPLER_RATIO": "0.5"}, 0.5),
        ({"OTEL_TRACES_SAMPLER_ARG": "0.25"}, 0.25),
        ({"OTEL_TRACES_SAMPLER_RATIO": "7"}, 1.0),
        ({"OTEL_TRACES_SAMPLER_RATIO": "-1"}, 0.0),
        ({"OTEL_TRACES_SAMPLER_RATIO": "lots"}, 0.1),
    ],
)
def test_sample_ratio(env: dict[str, str], ratio: float, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    assert TracingConfig.from_env(service_name="svc").sample_ratio == ratio


@pytest.mark.parametrize("key", ["OTEL_TRACES_SAMPLER_RATIO", "OTEL_TRACES_SAMPLER_ARG"])
def test_nan_sample_ratio_falls_back(
    key: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: "nan" parsed, passed the clamp, and crashed TraceIdRatioBased at startup."""
    monkeypatch.setenv(key, "nan")
    config = TracingConfig.from_env(service_name="svc")
    assert config.sample_ratio == 0.1
    assert f"invalid {key}" in capsys.readouterr().err
    tracing_module.build_sampler(config.sample_ratio, False)  # must not raise


def test_invalid_bool_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OTEL_PROPAGATOR_BAGGAGE", "sometimes")
    assert not TracingConfig.from_env(service_name="svc").enable_baggage
    assert "invalid OTEL_PROPAGATOR_BAGGAGE" in capsys.readouterr().err


def test_sampler() -> None:
    always = tracing_module.build_sampler(1.0, False)
    assert isinstance(always, ParentBased) and always._root is ALWAYS_ON
    assert tracing_module.build_sampler(0.0, False)._root is ALWAYS_OFF
    ratio = tracing_module.build_sampler(0.3, True)
    assert ratio._remote_parent_sampled is ratio._root  # re-sample sampled remote parents


def test_resource_attributes() -> None:
    resource = tracing_module.build_resource(
        TracingConfig(service_name="", service_version="v1", domain="iam", environment="prod")
    )
    attrs = dict(resource.attributes)
    assert attrs["service.name"] == UNKNOWN_SERVICE_NAME and attrs["service.version"] == "v1"
    assert attrs["service.namespace"] == "iam" and attrs["deployment.environment.name"] == "prod"
    bare = dict(tracing_module.build_resource(TracingConfig(service_name="s")).attributes)
    assert "service.namespace" not in bare and "deployment.environment.name" not in bare


def test_provider_swap_follows_old_tracers_and_flushes() -> None:
    tracer = new_tracer("early")  # obtained before / across installs
    first, second = InMemorySpanExporter(), InMemorySpanExporter()
    init_tracing(TracingConfig(service_name="a", endpoint="", sample_ratio=1.0), first)
    with tracer.start_as_current_span("one"):
        pass
    init_tracing(TracingConfig(service_name="b", endpoint="", sample_ratio=1.0), second)
    with tracer.start_as_current_span("two"):
        pass
    shutdown_tracing()
    assert [s.name for s in first.get_finished_spans()] == ["one"]
    assert [s.name for s in second.get_finished_spans()] == ["two"]
    assert active_tracing_config() is None
    # After shutdown the global provider records nothing but keeps the parent context.
    with tracer.start_as_current_span("dropped") as span:
        assert not span.is_recording()


def test_decorator_follows_the_provider_per_call() -> None:
    """Regression: start_as_current_span bound the tracer when the decorator was applied, so a
    function decorated at import (before tracing was installed) never produced spans."""

    @new_tracer("decorated").start_as_current_span("op")
    def op() -> bool:
        return trace.get_current_span().is_recording()

    assert op() is False  # no provider yet
    first, second = InMemorySpanExporter(), InMemorySpanExporter()
    init_tracing(TracingConfig(service_name="a", endpoint="", sample_ratio=1.0), first)
    assert op() is True
    init_tracing(TracingConfig(service_name="b", endpoint="", sample_ratio=1.0), second)
    assert op() is True
    shutdown_tracing()
    assert [s.name for s in first.get_finished_spans()] == ["op"]
    assert [s.name for s in second.get_finished_spans()] == ["op"]


def test_flush_tracing_keeps_the_provider_running() -> None:
    """An app's shutdown flushes, but other apps / gRPC servers sharing the provider keep
    recording (shutdown_tracing used to be called there)."""
    exporter = InMemorySpanExporter()
    init_tracing(TracingConfig(service_name="a", endpoint="", sample_ratio=1.0), exporter)
    tracer = new_tracer("shared")
    with tracer.start_as_current_span("before"):
        pass
    flush_tracing()
    assert [s.name for s in exporter.get_finished_spans()] == ["before"]
    with tracer.start_as_current_span("after") as span:
        assert span.is_recording()
    assert active_tracing_config() is not None
    shutdown_tracing()


def test_ensure_tracing_rules() -> None:
    provisional = ensure_tracing(
        TracingConfig(service_name=UNKNOWN_SERVICE_NAME, provisional=True, endpoint="")
    )
    assert provisional is not None
    configured = ensure_tracing(TracingConfig(service_name="svc", endpoint=""))
    assert configured is not provisional  # a provisional install is replaced
    assert ensure_tracing(TracingConfig(service_name="other", endpoint="")) is configured
    config = active_tracing_config()
    assert config is not None and config.service_name == "svc"


def test_consumer_provider_is_never_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    own = TracerProvider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: own)
    assert ensure_tracing(TracingConfig(service_name="svc", endpoint="")) is None


def test_initialized_log_and_otlp_exporter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "prod")
    capsys.readouterr()
    init_tracing(TracingConfig(service_name="svc", endpoint="localhost:4317", insecure=True))
    entry = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert entry["msg"] == "otel_tracing_initialized" and entry["exporter"] == "otlp-grpc"
    assert entry["endpoint"] == "localhost:4317" and entry["insecure"] is True
    shutdown_tracing()


def test_init_tracing_from_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    init_tracing_from_env()
    assert "without a service name" in capsys.readouterr().err
    config = active_tracing_config()
    assert config is not None and config.provisional
    monkeypatch.setenv("OTEL_SERVICE_NAME", "named")
    init_tracing_from_env()
    config = active_tracing_config()
    assert config is not None and config.service_name == "named"


def test_helpers() -> None:
    exporter = InMemorySpanExporter()
    provider = init_tracing(
        TracingConfig(service_name="svc", endpoint="", sample_ratio=1.0), exporter
    )
    assert span_trace_id() == ""
    with new_tracer("svc").start_as_current_span("work"):
        trace_id = span_trace_id()
        assert len(trace_id) == 32
        headers = inject_trace_context({})
        assert headers["traceparent"].split("-")[1] == trace_id
        record_span_error(ValueError("x"), "lookup failed")
    assert span_trace_id(extract_trace_context(headers)) == trace_id
    provider.force_flush()
    (finished,) = exporter.get_finished_spans()
    assert finished.status.description == "lookup failed"
    assert not noop_tracer().start_span("n").is_recording()


def test_baggage_propagation_when_enabled() -> None:
    init_tracing(TracingConfig(service_name="svc", endpoint="", enable_baggage=True))
    from opentelemetry import baggage

    context = baggage.set_baggage("k", "v")
    assert "baggage" in inject_trace_context({}, context)


def test_setup_tracing_adds_the_middleware() -> None:
    app = FastAPI()
    provider = setup_tracing(app, service_name="svc")
    assert isinstance(provider, TracerProvider)
    assert any(m.cls.__name__ == "TracingMiddleware" for m in app.user_middleware)
