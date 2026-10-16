"""Emitted-metric compliance (Go ``validate.go`` / ``openmetrics.go``)."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry, Counter, Histogram, Summary

from fastapicommon.metrics.compliance import (
    Family,
    Options,
    Report,
    openmetrics_to_text,
    parse_text,
    validate,
    validate_families,
    validate_text,
)

CONST = 'domain="iam",environment="prod",service="event-consumer"'
AUTH = 'event_type="login",outcome="success"'


def _rules(text: str, options: Options | None = None) -> dict[str, list[str]]:
    report = validate_text(text, options)
    rules: dict[str, list[str]] = {}
    for finding in report.findings:
        rules.setdefault(finding.metric, []).append(f"{finding.severity}:{finding.rule}")
    return rules


def test_compliant_tier1_tier2_tier3_and_exempt() -> None:
    text = f"""# HELP platform_build_info x
# TYPE platform_build_info gauge
platform_build_info{{{CONST},version="v1"}} 1
# TYPE iam_auth_events_total counter
iam_auth_events_total{{{CONST},event_type="login",outcome="success"}} 1
# TYPE iam_event_consumer_lookup_seconds histogram
iam_event_consumer_lookup_seconds_bucket{{{CONST},cache="hit",le="0.1"}} 1
iam_event_consumer_lookup_seconds_bucket{{{CONST},cache="hit",le="+Inf"}} 1
iam_event_consumer_lookup_seconds_sum{{{CONST},cache="hit"}} 0.05
iam_event_consumer_lookup_seconds_count{{{CONST},cache="hit"}} 1
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 3
"""
    report = validate_text(text)
    assert report.ok(), str(report)
    assert report.checked == 4
    assert report.tiers == {
        "platform_build_info": "platform",
        "iam_auth_events_total": "domain",
        "iam_event_consumer_lookup_seconds": "service",
        "process_cpu_seconds_total": "exempt",
    }
    assert {f.rule for f in report.warnings()} == {"status"}
    assert "4 metrics checked: 0 errors, 2 warnings" in str(report)


def test_strict_mode_and_allowed_owners() -> None:
    text = f"""# TYPE platform_build_info gauge
platform_build_info{{{CONST},version="v1"}} 1
# TYPE iam_auth_events_total counter
iam_auth_events_total{{{CONST},event_type="login",outcome="success"}} 1
"""
    strict = _rules(text, Options(strict=True))
    assert strict["platform_build_info"] == ["error:status"]
    allowed = _rules(text, Options(strict=True, allow_proposed_owners=("platform-gincommon",)))
    assert allowed["platform_build_info"] == ["warning:status"]
    assert allowed["iam_auth_events_total"] == ["error:status"]  # no owner: always an error


def test_namespace_type_and_suffix_rules() -> None:
    text = f"""# TYPE Bad_Name gauge
Bad_Name 1
# TYPE platform_unknown_total counter
platform_unknown_total{{{CONST}}} 1
# TYPE stray_total counter
stray_total 1
# TYPE iam_other_service_hits_total counter
iam_other_service_hits_total{{{CONST}}} 1
# TYPE iam_event_consumer_hits counter
iam_event_consumer_hits{{domain="workflow",environment="prod",service="event-consumer"}} 1
# TYPE iam_event_consumer_latency summary
iam_event_consumer_latency_sum{{{CONST}}} 1
iam_event_consumer_latency_count{{{CONST}}} 1
# TYPE platform_http_active_requests counter
platform_http_active_requests{{{CONST},route="/x"}} 1
# TYPE http_requests_total counter
http_requests_total 1
"""
    rules = _rules(text)
    assert rules["Bad_Name"] == ["error:name-format"]
    assert rules["platform_unknown_total"] == ["error:namespace"]
    assert rules["stray_total"] == ["error:namespace"]
    assert rules["iam_other_service_hits_total"] == ["error:namespace"]
    assert set(rules["iam_event_consumer_hits"]) == {"error:namespace", "error:suffix"}
    assert rules["iam_event_consumer_latency"] == ["error:type"]
    assert "error:type" in rules["platform_http_active_requests"]
    assert rules["http_requests_total"] == ["error:removed"]


def test_tier3_without_service_label_is_a_namespace_error() -> None:
    rules = _rules(
        "# TYPE iam_event_consumer_hits_total counter\niam_event_consumer_hits_total 1\n"
    )
    assert rules["iam_event_consumer_hits_total"] == ["error:namespace"]


def test_label_rules() -> None:
    text = f"""# TYPE platform_http_active_requests gauge
platform_http_active_requests{{{CONST},route="/a",extra="x"}} 1
platform_http_active_requests{{domain="iam",service="event-consumer",route="has space"}} 1
platform_http_active_requests{{{CONST}}} 1
# TYPE iam_event_consumer_hits_total counter
iam_event_consumer_hits_total{{{CONST},user_id="u1",Bad="x",ok="y"}} 1
# TYPE iam_auth_events_total counter
iam_auth_events_total{{domain="workflow",environment="prod",service="s",{AUTH}}} 1
"""
    rules = _rules(text)
    assert set(rules["platform_http_active_requests"]) == {
        "warning:status",
        "error:label-vocabulary",
        "error:label-value",
        "error:required-label",
    }
    assert set(rules["iam_event_consumer_hits_total"]) == {
        "error:prohibited-label",
        "error:label-name",
    }
    assert "error:namespace" in rules["iam_auth_events_total"]


def test_cardinality_limit() -> None:
    routes = "\n".join(
        f'platform_http_active_requests{{{CONST},route="/r{i}"}} 1' for i in range(1001)
    )
    rules = _rules(f"# TYPE platform_http_active_requests gauge\n{routes}\n")
    assert "error:cardinality" in rules["platform_http_active_requests"]


def test_deprecated_legacy_is_a_warning() -> None:
    family = Family("http_requests_total", "counter", [{}])
    import json

    from fastapicommon.metrics.registry import default_registry, load_registry, registry_bytes

    document = json.loads(registry_bytes())
    for entry in document["metrics"]:
        if entry["name"] == "http_requests_total":
            entry["status"] = "deprecated"
    report = validate_families([family], observability_registry=load_registry(json.dumps(document)))
    assert [f.rule for f in report.findings] == ["deprecated"]
    assert default_registry().lookup("http_requests_total") is not None


def test_created_companions_are_classified_with_their_parent() -> None:
    text = f"""# TYPE iam_event_consumer_hits_total counter
iam_event_consumer_hits_total{{{CONST}}} 1
# TYPE iam_event_consumer_hits_created gauge
iam_event_consumer_hits_created{{{CONST}}} 1.7e9
# TYPE iam_event_consumer_lookup_seconds histogram
iam_event_consumer_lookup_seconds_count{{{CONST}}} 1
# TYPE iam_event_consumer_lookup_seconds_created gauge
iam_event_consumer_lookup_seconds_created{{{CONST}}} 1.7e9
# TYPE iam_event_consumer_orphan_created gauge
iam_event_consumer_orphan_created{{{CONST}}} 1
# TYPE iam_event_consumer_mismatch_created gauge
iam_event_consumer_mismatch_created{{{CONST},x="1"}} 1
# TYPE iam_event_consumer_mismatch_total counter
iam_event_consumer_mismatch_total{{{CONST}}} 1
"""
    report = validate_text(text)
    assert report.tiers["iam_event_consumer_hits_created"] == "exempt"
    assert report.tiers["iam_event_consumer_lookup_seconds_created"] == "exempt"
    assert report.tiers["iam_event_consumer_orphan_created"] == "service"
    assert report.tiers["iam_event_consumer_mismatch_created"] == "service"


def test_text_parser() -> None:
    families = {
        f.name: f
        for f in parse_text(
            "# HELP a_total doc\n# TYPE a_total counter\n"
            'a_total{x="q\\"uote\\\\ \\n"} 1 1700000000\n'
            "untyped_sample 2\n"
            '# TYPE s summary\ns{quantile="0.5"} 1\ns_sum 1\ns_count 1\n\n'
        )
    }
    assert families["a_total"].series == [{"x": 'q"uote\\ \n'}]
    assert families["untyped_sample"].type == "untyped"
    assert families["s"].series == [{}]
    for bad in ("{x} 1", 'a{x="1} 1', "a{x=1} 1", "a", "a nope", "# TYPE a\n"):
        with pytest.raises(ValueError):
            parse_text(bad)


def test_a_prohibited_label_is_reported_once_per_metric() -> None:
    """Every series carries the label, but the finding is the metric's, not each series'."""
    text = f"""# TYPE iam_event_consumer_jobs_total counter
iam_event_consumer_jobs_total{{{CONST},tenant_id="t1"}} 1
iam_event_consumer_jobs_total{{{CONST},tenant_id="t2"}} 1
"""
    assert _rules(text)["iam_event_consumer_jobs_total"] == ["error:prohibited-label"]


def test_openmetrics_keeps_untyped_samples_and_skips_blank_lines() -> None:
    """A sample with no ``# TYPE`` line belongs to no family: it is kept unchanged (Go
    ``openMetricsToText``), and blank lines are dropped."""
    text = "# TYPE c counter\n\nc_total 1\n\norphan_total 2\n# EOF\n"
    assert openmetrics_to_text(text) == "# TYPE c_total counter\nc_total 1\norphan_total 2\n"


def test_openmetrics_is_normalised() -> None:
    text = """# TYPE c counter
# HELP c a counter
c_total{a="1"} 1 1700000000.5 # {trace_id="t"} 1
c_created{a="1"} 1.7e9
# TYPE i info
i_info{v="1"} 1
# TYPE st stateset
st{st="a"} 1
# TYPE gh gaugehistogram
gh_bucket{le="+Inf"} 1
gh_gcount 1
gh_gsum 1
# TYPE u unknown
u 1
# UNIT c seconds
# EOF
"""
    normalised = openmetrics_to_text(text)
    assert "# TYPE c_total counter" in normalised and "c_created" not in normalised
    assert 'c_total{a="1"} 1\n' in normalised
    assert "# TYPE i_info gauge" in normalised and "# TYPE st gauge" in normalised
    assert "gh_count 1" in normalised and "gh_sum 1" in normalised
    assert "# TYPE u untyped" in normalised and "# EOF" not in normalised
    names = {f.name: f.type for f in parse_text(text)}
    assert names == {
        "c_total": "counter",
        "i_info": "gauge",
        "st": "gauge",
        "gh": "histogram",
        "u": "untyped",
    }


def test_validate_collector_registry() -> None:
    registry = CollectorRegistry()
    Counter("iam_event_consumer_hits_total", "x", registry=registry).inc()
    Summary("iam_event_consumer_latency_seconds", "x", registry=registry).observe(1)
    Histogram("platform_new_seconds", "x", registry=registry).observe(1)
    report = validate(registry)
    errors = {(f.metric, f.rule) for f in report.errors()}
    assert ("iam_event_consumer_latency_seconds", "type") in errors
    assert ("platform_new_seconds", "namespace") in errors
    # Unlabelled Tier 3 metrics lack the service label: required-label errors are expected.
    assert ("iam_event_consumer_hits_total", "namespace") in errors


def test_validate_defaults_to_the_served_registry() -> None:
    assert isinstance(validate(), Report)


def test_gather_failure_is_reported() -> None:
    class Broken:
        def collect(self) -> list[object]:
            raise RuntimeError("collector exploded")

    registry = CollectorRegistry()
    registry.register(Broken())  # type: ignore[arg-type]
    report = validate(registry)
    assert [(f.rule, f.metric) for f in report.findings] == [("gather", "(gather)")]
    assert not report.ok()
