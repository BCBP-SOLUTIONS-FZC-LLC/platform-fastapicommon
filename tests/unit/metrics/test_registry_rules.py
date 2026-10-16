"""Registry self-check (Go ``obsregistry.Parse``) and the construction rules
(``CheckCustom``, ``SharedMetric``, ``ServicePrefix``)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from fastapicommon.metrics.registry import (
    RegistryError,
    default_registry,
    parse_registry,
    registry_bytes,
    service_prefix,
    suffix_error,
)


def _document() -> dict[str, Any]:
    return copy.deepcopy(json.loads(registry_bytes()))


def _metric(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(m for m in document["metrics"] if m["name"] == name)


def _problems(document: Any) -> str:
    with pytest.raises(RegistryError) as info:
        parse_registry(json.dumps(document))
    return str(info.value)


def test_vendored_registry_parses_strictly() -> None:
    registry = parse_registry(registry_bytes())
    assert registry.schema_version == 1
    assert registry.lookup("platform_http_requests_total") is not None
    assert default_registry().required_labels["platform"] == ("domain", "service", "environment")


def test_invalid_json_and_shape() -> None:
    with pytest.raises(ValueError, match="decode registry"):
        parse_registry("{not json")
    assert "must be a JSON object" in _problems([])
    document = _document()
    document["surprise"] = 1
    del document["metrics"]
    problems = _problems(document)
    assert "unknown field 'surprise'" in problems and "metrics is required" in problems


def test_unknown_fields_on_labels_and_metrics() -> None:
    document = _document()
    document["labels"]["route"]["colour"] = "red"
    _metric(document, "platform_build_info")["owners"] = []
    del _metric(document, "platform_http_panics_total")["type"]
    problems = _problems(document)
    assert "label 'route': unknown field 'colour'" in problems
    assert "unknown field 'owners'" in problems
    assert "type is required" in problems


def test_document_level_rules() -> None:
    document = _document()
    document["schema_version"] = 2
    document["labels"]["Bad-Label"] = {"description": "x", "cardinality": "x", "values": ["a"]}
    document["labels"]["user_id"] = {"description": "x", "cardinality": "x", "values": ["a"]}
    document["labels"]["nodesc"] = {"values": ["a"]}
    document["labels"]["both"] = {
        "description": "x",
        "cardinality": "x",
        "values": ["a"],
        "pattern": "^a$",
    }
    document["labels"]["neither"] = {"description": "x", "cardinality": "x"}
    document["const_labels"] = ["domain", "service", "ghost"]
    document["required_labels"] = {"platform": ["route"], "domain": []}
    problems = _problems(document)
    for expected in (
        "schema_version 2 is not supported",
        "label 'Bad-Label': name is not snake_case",
        "label 'user_id': name is prohibited",
        "label 'nodesc': description and cardinality are required",
        "label 'both': set values or pattern, not both",
        "label 'neither': values or pattern is required",
        "const label 'ghost' is not defined in labels",
        "const_labels must include 'environment'",
        "required_labels.service is missing",
        "required label 'route' (platform) must be a const label",
    ):
        assert expected in problems, expected


def test_shared_entry_rules() -> None:
    document = _document()
    entry = _metric(document, "platform_http_requests_total")
    entry.update(status="deprecated", aggregation="", labels=["route", "domain", "nope"])
    entry["label_values"] = {"method": ["GET"], "route": ["no spaces allowed"]}
    histogram = _metric(document, "platform_http_request_duration_seconds")
    histogram["buckets"] = ""
    _metric(document, "platform_grpc_panics_total").update(name="Platform-Bad", help="")
    _metric(document, "platform_http_panics_total")["status"] = "weird"
    _metric(document, "platform_http_active_requests")["buckets"] = "nope"
    _metric(document, "platform_http_active_requests")["type"] = "histogram"
    domain_entry = _metric(document, "iam_auth_events_total")
    domain_entry["domain"] = "hr"
    other_domain = _metric(document, "iam_tenant_resolution_total")
    other_domain["domain"] = "workflow"
    document["metrics"].append(
        {**_metric(document, "platform_build_info"), "name": "build_thing", "tier": "platform"}
    )
    document["metrics"].append(_metric(document, "platform_build_info"))
    document["metrics"].append({**_metric(document, "platform_build_info"), "tier": "galaxy"})
    problems = _problems(document)
    for expected in (
        "shared metrics are retired by deleting the entry, not by status deprecated",
        "aggregation expectations are required",
        "label 'domain' is a const label",
        "label 'nope' is not in the label vocabulary",
        "label_values for 'method', which is not one of its labels",
        "label_values route='no spaces allowed' is outside the label vocabulary",
        "shared histograms must name a bucket preset",
        "'Platform-Bad': name is not snake_case",
        "'Platform-Bad': help is required",
        "unknown status 'weird'",
        "unknown bucket preset 'nope'",
        "histograms must end in _seconds",
        "unknown domain 'hr'",
        "domain-tier metrics must be named workflow_*",
        "platform-tier metrics must be named platform_*",
        "'platform_build_info': duplicate entry",
        "tier 'galaxy' cannot be registered",
    ):
        assert expected in problems, expected


def test_legacy_entry_rules() -> None:
    document = _document()
    legacy = _metric(document, "http_requests_total")
    legacy.update(status="approved", replacements=["ghost_total", "build_info"], sunset="")
    _metric(document, "grpc_requests_total")["replacements"] = []
    problems = _problems(document)
    for expected in (
        "legacy metrics must be deprecated or removed",
        "replacement 'ghost_total' is not in the registry",
        "replacement 'build_info' is itself legacy",
        "sunset is required for legacy metrics",
        "replacements are required for legacy metrics",
    ):
        assert expected in problems, expected


@pytest.mark.parametrize(
    ("metric_type", "name", "unit", "expected"),
    [
        ("counter", "x_total", "", ""),
        ("counter", "x", "", "counters must end in _total"),
        ("histogram", "x_seconds", "seconds", ""),
        ("histogram", "x_bytes", "bytes", ""),
        ("histogram", "x_bytes", "", "histograms must end in _seconds"),
        ("histogram", "x_seconds", "meters", "histogram unit 'meters' is not supported"),
        ("gauge", "x", "", ""),
        ("gauge", "x_total", "", "gauges must not end in _total"),
        ("summary", "x", "", "unknown type 'summary'"),
    ],
)
def test_suffix_error(metric_type: str, name: str, unit: str, expected: str) -> None:
    assert suffix_error(metric_type, name, unit).startswith(expected)
    assert bool(suffix_error(metric_type, name, unit)) == bool(expected)


@pytest.mark.parametrize(
    ("domain", "service", "prefix"),
    [
        ("iam", "event-consumer", "iam_event_consumer_"),
        ("iam", "iam-event-consumer", "iam_event_consumer_"),
        ("iam", "Event  Consumer!", "iam_event_consumer_"),
        ("tender_management", "bid-api", "tender_management_bid_api_"),
    ],
)
def test_service_prefix(domain: str, service: str, prefix: str) -> None:
    assert service_prefix(domain, service) == prefix


def test_check_custom() -> None:
    registry = default_registry()
    registry.check_custom(
        "counter", "iam_event_consumer_hits_total", ["cache"], "iam", "event-consumer"
    )
    registry.check_custom("gauge", "anything_goes", [], "", "")  # names only without a domain
    cases = [
        (("counter", "Bad-Name"), "snake_case"),
        (("counter", "platform_http_requests_total"), "create it from the registry"),
        (("counter", "http_requests_total"), "legacy name; emit its replacement"),
        (("counter", "platform_new_total"), "must be ratified"),
        (("counter", "python_thing_total"), "reserved for runtime collectors"),
        (("counter", "iam_event_consumer_hits"), "counters must end in _total"),
        (("counter", "iam_event_consumer_hits_total", ["Bad"]), "must be snake_case"),
        (("counter", "iam_event_consumer_hits_total", ["user_id"]), "prohibited"),
        (("counter", "iam_event_consumer_hits_total", ["service"]), "injected centrally"),
        (("counter", "iam_other_hits_total"), "must be named iam_event_consumer_<metric>"),
    ]
    for args, message in cases:
        metric_type, name, *labels = args
        with pytest.raises(ValueError, match=message):
            registry.check_custom(
                metric_type, name, labels[0] if labels else [], "iam", "event-consumer"
            )


def test_shared_metric() -> None:
    registry = default_registry()
    assert registry.shared_metric("iam_auth_events_total", "counter", "iam").domain == "iam"
    assert registry.shared_metric("platform_queue_depth", "gauge", "workflow").tier == "platform"
    for args, message in (
        (("ghost_total", "counter", "iam"), "not in the Platform Observability Registry"),
        (("http_requests_total", "counter", "iam"), "is a legacy metric, not a shared one"),
        (("platform_queue_depth", "counter", "iam"), "is a gauge, not a counter"),
        (("iam_auth_events_total", "counter", "workflow"), "belongs to domain 'iam'"),
    ):
        with pytest.raises(ValueError, match=message):
            registry.shared_metric(*args)


def test_lookups_and_label_helpers() -> None:
    registry = default_registry()
    assert registry.domain_of("tender_management_bids_total") == "tender_management"
    assert registry.domain_of("hr_x_total") == ""
    assert registry.is_exempt("process_cpu_seconds_total")
    assert registry.is_prohibited_label("tenant_uuid")
    assert not registry.is_prohibited_label("route")
    entry = registry.lookup("platform_messages_published_total")
    assert entry is not None
    assert registry.allows(entry, "outcome", "error")
    assert not registry.allows(entry, "outcome", "failure")  # narrowed by label_values
    assert registry.allows(None, "unregistered_label", "anything")
    assert registry.label_pattern("service").startswith("^")
    assert registry.label_pattern("method") == ""
