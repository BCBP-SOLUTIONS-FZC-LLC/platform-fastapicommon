from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastapicommon.metrics.registry import default_registry, load_registry, registry_bytes

#: The Go source of truth, when the workspace's sibling checkout is present.
_GINCOMMON_REGISTRY = (
    Path(__file__).resolve().parents[5]
    / "platform-gincommon"
    / "internal"
    / "obsregistry"
    / "registry.json"
)


@pytest.mark.skipif(not _GINCOMMON_REGISTRY.exists(), reason="platform-gincommon not checked out")
def test_vendored_registry_is_identical_to_gincommon() -> None:
    assert registry_bytes() == _GINCOMMON_REGISTRY.read_bytes(), (
        "src/fastapicommon/metrics/registry.json drifted from platform-gincommon's "
        "internal/obsregistry/registry.json — re-copy it (Go is the source of truth)"
    )


def test_platform_entries_used_by_this_package_exist_with_expected_types() -> None:
    registry = default_registry()
    expected = {
        "platform_build_info": "gauge",
        "platform_http_requests_total": "counter",
        "platform_http_request_duration_seconds": "histogram",
        "platform_http_request_size_bytes": "histogram",
        "platform_http_response_size_bytes": "histogram",
        "platform_http_active_requests": "gauge",
        "platform_http_panics_total": "counter",
        "platform_http_rate_limited_requests_total": "counter",
        "platform_http_request_timeouts_total": "counter",
        "platform_grpc_server_handled_total": "counter",
        "platform_grpc_server_handling_seconds": "histogram",
        "platform_grpc_server_request_size_bytes": "histogram",
        "platform_grpc_server_response_size_bytes": "histogram",
        "platform_grpc_server_inflight_requests": "gauge",
        "platform_grpc_rate_limited_requests_total": "counter",
        "platform_grpc_panics_total": "counter",
    }
    for name, metric_type in expected.items():
        spec = registry.lookup(name)
        assert spec is not None, name
        assert spec.type == metric_type
        assert spec.tier == "platform"
    grpc_handled = registry.lookup("platform_grpc_server_handled_total")
    assert grpc_handled is not None
    assert grpc_handled.labels == ("grpc_method", "grpc_code", "status_class", "error_class")
    assert registry.lookup("nonexistent_metric") is None


def test_const_labels_are_the_identity_labels() -> None:
    assert set(default_registry().const_labels) == {"domain", "service", "environment"}


def test_normalize_environment_trims_and_lowercases() -> None:
    registry = default_registry()
    assert registry.normalize_environment("  PROD ") == "prod"
    assert registry.normalize_environment("local") == "local"


@pytest.mark.parametrize("environment", ["production", "development", "qa", ""])
def test_normalize_environment_rejects_values_outside_the_vocabulary(environment: str) -> None:
    with pytest.raises(ValueError, match="environment"):
        default_registry().normalize_environment(environment)


def test_check_identity() -> None:
    registry = default_registry()
    assert registry.check_identity("iam", "event-consumer", "Staging") == "staging"
    assert registry.check_identity("iam", "event_consumer", "dev") == "dev"
    with pytest.raises(ValueError, match="domain 'hr'"):
        registry.check_identity("hr", "svc", "prod")
    with pytest.raises(ValueError, match="domain ''"):
        registry.check_identity("", "svc", "prod")
    with pytest.raises(ValueError, match="service 'Bad Name'"):
        registry.check_identity("iam", "Bad Name", "prod")


def test_label_patterns_use_full_match_semantics() -> None:
    service = default_registry().labels["service"]
    assert service.allows("svc")
    # Python's `$` alone would accept a trailing newline; RE2 (Go) does not.
    assert not service.allows("svc\n")


def test_domain_vocabulary_is_pinned() -> None:
    # Startup rejects any other OBSERVABILITY_DOMAIN, and consumers rely on these values (same pin
    # as gincommon's TestDomains_Vocabulary), so removing or renaming one is a breaking change.
    assert default_registry().domains() == (
        "iam",
        "workflow",
        "billing",
        "tender_management",
        "notifications",
        "documents",
        "bff",
        "portal",
    )
    # The startup check (OBSERVABILITY_DOMAIN) accepts the newest value.
    default_registry().check_identity("portal", "web-app", "prod")


def test_registry_without_identity_vocabularies_accepts_nothing() -> None:
    document = json.loads(registry_bytes())
    del document["labels"]["environment"]
    del document["labels"]["domain"]
    registry = load_registry(json.dumps(document))
    assert registry.environments() == ()
    assert registry.domains() == ()
    with pytest.raises(ValueError, match="environment"):
        registry.normalize_environment("prod")
