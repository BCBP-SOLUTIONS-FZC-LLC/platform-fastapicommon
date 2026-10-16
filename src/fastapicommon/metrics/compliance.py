"""Enterprise Platform Observability Standard compliance checks for emitted metrics.

Python port of platform-gincommon's ``internal/obsregistry/validate.go`` (exposed to Go services
as ``pkg/obsregistry.Validate`` and ``metricslint check``): every metric family of a scrape is
classified into a tier and checked for naming, suffixes, type, required labels, the registry's
label vocabulary and values, prohibited labels and cardinality. Consuming services run it in
their own CI::

    from fastapicommon.metrics import metrics_registry
    from fastapicommon.metrics.compliance import validate

    def test_metrics_compliance(client):
        client.get("/widgets/me")  # drive the instrumented code paths once
        report = validate(metrics_registry())
        assert report.ok(), "\\n".join(map(str, report.errors()))

:func:`validate` checks exactly what ``/metrics`` serves: the registry rendered in the Prometheus
text format. :func:`validate_text` takes a scrape (Prometheus text 0.0.4 or OpenMetrics, the
latter recognised by its ``# EOF`` line and normalised first, as Go does).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, generate_latest

from fastapicommon.metrics.registry import (
    COUNTER,
    GAUGE,
    HISTOGRAM,
    LABEL_NAME_RE,
    NAME_RE,
    STATUS_PROPOSED,
    STATUS_REMOVED,
    TIER_DOMAIN,
    TIER_EXEMPT,
    TIER_LEGACY,
    TIER_SERVICE,
    MetricSpec,
    ObservabilityRegistry,
    default_registry,
    service_prefix,
    suffix_error,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

RULE_NAME_FORMAT = "name-format"
RULE_NAMESPACE = "namespace"
RULE_TYPE = "type"
RULE_SUFFIX = "suffix"
RULE_REQUIRED_LABEL = "required-label"
RULE_LABEL_VOCABULARY = "label-vocabulary"
RULE_PROHIBITED_LABEL = "prohibited-label"
RULE_LABEL_VALUE = "label-value"
RULE_CARDINALITY = "cardinality"
RULE_STATUS = "status"
RULE_DEPRECATED = "deprecated"
RULE_REMOVED = "removed"
RULE_LABEL_NAME = "label-name"
RULE_GATHER = "gather"


@dataclass(frozen=True)
class Finding:
    """One compliance problem with an emitted metric."""

    severity: str
    rule: str
    metric: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity} [{self.rule}] {self.metric}: {self.message}"


@dataclass(frozen=True)
class Options:
    """Validation options (Go ``obsregistry.Options``).

    ``strict`` reports metrics still awaiting ratification (status ``proposed``) as errors
    instead of warnings, except entries owned by a library in ``allow_proposed_owners`` (e.g.
    ``"platform-gincommon"``), so a service can be strict about what it emits itself.
    """

    strict: bool = False
    allow_proposed_owners: tuple[str, ...] = ()


@dataclass
class Report:
    """The outcome of a validation."""

    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    tiers: dict[str, str] = field(default_factory=dict)

    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    def ok(self) -> bool:
        return not self.errors()

    def __str__(self) -> str:
        lines = [str(f) for f in self.findings]
        lines.append(
            f"{self.checked} metrics checked: {len(self.errors())} errors, "
            f"{len(self.warnings())} warnings"
        )
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------
# Metric families (the subset of Go's dto.MetricFamily the rules need)
# --------------------------------------------------------------------------------------------

_SERIES_SUFFIXES = {
    "histogram": ("_bucket", "_sum", "_count"),
    "gaugehistogram": ("_bucket", "_sum", "_count"),
    "summary": ("_sum", "_count"),
}


@dataclass
class Family:
    """A metric family: its exposed name, type and the label sets of its series (``le`` and
    ``quantile`` removed, so one histogram series is one label set, as in Go)."""

    name: str
    type: str
    series: list[dict[str, str]] = field(default_factory=list)

    def add(self, labels: dict[str, str]) -> None:
        labels = {k: v for k, v in labels.items() if k not in ("le", "quantile")}
        if labels not in self.series:
            self.series.append(labels)

    def first_label_value(self, name: str) -> str:
        for labels in self.series:
            if name in labels:
                return labels[name]
        return ""

    def label_sets(self) -> set[tuple[tuple[str, str], ...]]:
        return {tuple(sorted(labels.items())) for labels in self.series}


def parse_text(text: str) -> list[Family]:
    """Parse a Prometheus text-format (0.0.4) exposition into families. OpenMetrics text is
    normalised to the Prometheus text format first. Raises ``ValueError`` on malformed input."""
    if any(line.strip() == "# EOF" for line in text.splitlines()):
        text = openmetrics_to_text(text)
    types: dict[str, str] = {}
    families: dict[str, Family] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 4 and parts[1] == "TYPE":
                types[parts[2]] = parts[3].strip()
                families.setdefault(parts[2], Family(parts[2], parts[3].strip()))
            elif len(parts) >= 3 and parts[1] == "TYPE":
                raise ValueError(f"line {number}: TYPE without a type")
            continue
        name, labels = _parse_sample(line, number)
        family_name = _family_of(name, types)
        family = families.setdefault(
            family_name, Family(family_name, types.get(family_name, "untyped"))
        )
        family.add(labels)
    return list(families.values())


def _family_of(sample: str, types: dict[str, str]) -> str:
    if sample in types:
        return sample
    for family_type in ("histogram", "gaugehistogram", "summary"):
        for suffix in _SERIES_SUFFIXES[family_type]:
            base = sample.removesuffix(suffix)
            if base != sample and types.get(base) == family_type:
                return base
    return sample


def _parse_sample(line: str, number: int) -> tuple[str, dict[str, str]]:
    """``name{a="1",b="x"} value [timestamp]`` → (name, labels)."""
    i = 0
    while i < len(line) and (line[i].isalnum() or line[i] in "_:"):
        i += 1
    name = line[:i]
    if not name:
        raise ValueError(f"line {number}: expected a metric name: {line!r}")
    labels: dict[str, str] = {}
    if i < len(line) and line[i] == "{":
        i += 1
        while True:
            while i < len(line) and line[i] in " ,":
                i += 1
            if i < len(line) and line[i] == "}":
                i += 1
                break
            start = i
            while i < len(line) and (line[i].isalnum() or line[i] == "_"):
                i += 1
            label = line[start:i]
            if not label or line[i : i + 2] != '="':
                raise ValueError(f"line {number}: malformed label set: {line!r}")
            i += 2
            value: list[str] = []
            while True:
                if i >= len(line):
                    raise ValueError(f"line {number}: unterminated label value: {line!r}")
                ch = line[i]
                if ch == "\\" and i + 1 < len(line):
                    value.append(
                        {"n": "\n", "\\": "\\", '"': '"'}.get(line[i + 1], "\\" + line[i + 1])
                    )
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                value.append(ch)
                i += 1
            labels[label] = "".join(value)
    rest = line[i:].split()
    if not rest:
        raise ValueError(f"line {number}: sample without a value: {line!r}")
    try:
        float(rest[0])
    except ValueError as exc:
        raise ValueError(f"line {number}: invalid sample value {rest[0]!r}") from exc
    return name, labels


def openmetrics_to_text(text: str) -> str:
    """Rewrite OpenMetrics text as Prometheus text (Go ``openMetricsToText``): counter families
    take their ``_total`` sample name, info families their ``_info`` name as gauges, stateset
    families become gauges, unknown becomes untyped, gaugehistogram ``_gcount``/``_gsum``
    become ``_count``/``_sum``; ``_created`` samples, ``# UNIT`` and ``# EOF`` are dropped, and
    exemplars and timestamps stripped."""
    types: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "#" and parts[1] == "TYPE":
            types[parts[2]] = parts[3]
    renamed = {"counter": "_total", "info": "_info"}
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) < 3 or parts[1] not in ("HELP", "TYPE"):
                continue
            family = parts[2]
            om_type = types.get(family, "unknown")
            name = family + renamed.get(om_type, "")
            if parts[1] == "TYPE":
                prom_type = {
                    "counter": "counter",
                    "gauge": "gauge",
                    "histogram": "histogram",
                    "gaugehistogram": "histogram",
                    "summary": "summary",
                    "info": "gauge",
                    "stateset": "gauge",
                }.get(om_type, "untyped")
                out.append(f"# TYPE {name} {prom_type}")
            else:
                out.append(f"# HELP {name} {parts[3] if len(parts) > 3 else ''}".rstrip())
            continue
        sample = line.split(" # ", 1)[0]  # exemplar
        head, _, _ = sample.partition("}")
        sample_name = head.split("{", 1)[0].split(" ", 1)[0]
        base = _om_family(sample_name, types)
        if base is not None and sample_name == base + "_created":
            continue
        if base is not None and types.get(base) == "gaugehistogram":
            sample = sample.replace(base + "_gcount", base + "_count", 1).replace(
                base + "_gsum", base + "_sum", 1
            )
        # Keep "name{labels} value", dropping a timestamp.
        if "}" in sample:
            labels_part, _, value_part = sample.partition("}")
            fields = value_part.split()
            sample = labels_part + "} " + (fields[0] if fields else "")
        else:
            fields = sample.split()
            sample = " ".join(fields[:2])
        out.append(sample)
    return "\n".join(out) + "\n"


def _om_family(sample: str, types: dict[str, str]) -> str | None:
    if sample in types:
        return sample
    for suffix in ("_total", "_created", "_info", "_bucket", "_count", "_sum", "_gcount", "_gsum"):
        base = sample.removesuffix(suffix)
        if base != sample and base in types:
            return base
    return None


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


def validate(
    registry: CollectorRegistry | None = None,
    options: Options | None = None,
    *,
    observability_registry: ObservabilityRegistry | None = None,
) -> Report:
    """Validate everything ``registry`` (default: the one ``/metrics`` serves,
    :func:`fastapicommon.metrics.metrics_registry`) exposes, rendered exactly as ``/metrics``
    renders it. A collector that fails while collecting is reported as a ``gather`` error (the
    exposition cannot be partially rendered, so no other family is checked then)."""
    if registry is None:
        from fastapicommon.metrics.platform import metrics_registry

        registry = metrics_registry()
    try:
        text = generate_latest(registry).decode()
    except Exception as exc:
        report = Report()
        report.findings.append(
            Finding(SEVERITY_ERROR, RULE_GATHER, "(gather)", f"{type(exc).__name__}: {exc}")
        )
        return report
    return validate_text(text, options, observability_registry=observability_registry)


def validate_text(
    text: str,
    options: Options | None = None,
    *,
    observability_registry: ObservabilityRegistry | None = None,
) -> Report:
    """Validate a Prometheus text-format or OpenMetrics scrape. Raises ``ValueError`` when the
    text cannot be parsed."""
    return validate_families(parse_text(text), options, observability_registry)


def validate_families(
    families: Iterable[Family],
    options: Options | None = None,
    observability_registry: ObservabilityRegistry | None = None,
) -> Report:
    """Check families against the standard (Go ``Registry.ValidateFamilies``)."""
    reg = observability_registry or default_registry()
    opts = options or Options()
    ordered = sorted(families, key=lambda f: f.name)
    by_name = {f.name: f for f in ordered}
    report = Report()
    for family in ordered:
        report.checked += 1
        if _is_created_companion(family, by_name):
            report.tiers[family.name] = TIER_EXEMPT
            continue
        _validate_family(reg, report, family, opts)
    return report


def _is_created_companion(family: Family, by_name: dict[str, Family]) -> bool:
    """prometheus_client's ``<metric>_created`` family: a gauge or untyped ``X_created`` whose
    series carry exactly the label sets of the counter ``X_total`` or histogram/summary ``X``."""
    base = family.name.removesuffix("_created")
    if base == family.name or family.type not in ("gauge", "untyped"):
        return False
    parent = by_name.get(base + "_total")
    if parent is None or parent.type != "counter":
        parent = by_name.get(base)
        if parent is None or parent.type not in ("histogram", "gaugehistogram", "summary"):
            return False
    return family.label_sets() == parent.label_sets()


def _family_type(family: Family) -> str | None:
    return {
        "counter": COUNTER,
        "gauge": GAUGE,
        "histogram": HISTOGRAM,
        "gaugehistogram": HISTOGRAM,
    }.get(family.type)


def _validate_family(
    reg: ObservabilityRegistry, report: Report, family: Family, opts: Options
) -> None:
    name = family.name

    def add(severity: str, rule: str, message: str) -> None:
        report.findings.append(Finding(severity, rule, name, message))

    if reg.is_exempt(name):
        report.tiers[name] = TIER_EXEMPT
        return
    if not NAME_RE.fullmatch(name):
        add(SEVERITY_ERROR, RULE_NAME_FORMAT, "name must be snake_case [a-z0-9_]")
        return
    metric_type = _family_type(family)
    if metric_type is None:
        add(
            SEVERITY_ERROR,
            RULE_TYPE,
            f"type {family.type.upper()} is not covered by the standard (use counter, gauge or "
            "histogram)",
        )
        return

    entry = reg.lookup(name)
    if entry is not None and entry.tier == TIER_LEGACY:
        report.tiers[name] = TIER_LEGACY
        replacements = ", ".join(entry.replacements)
        if entry.status == STATUS_REMOVED:
            add(
                SEVERITY_ERROR,
                RULE_REMOVED,
                f"legacy metric was removed; emit {replacements} instead",
            )
        else:
            add(
                SEVERITY_WARNING,
                RULE_DEPRECATED,
                "legacy metric; migrate dashboards, alerts, recording rules, SLOs and HPAs to "
                f"{replacements}. Sunset: {entry.sunset}",
            )
        return

    service = family.first_label_value("service")
    if entry is not None:
        tier = entry.tier
        if entry.type != metric_type:
            add(
                SEVERITY_ERROR,
                RULE_TYPE,
                f"registry defines a {entry.type}, emitted as {metric_type}",
            )
        if entry.status == STATUS_PROPOSED:
            severity = SEVERITY_WARNING
            if opts.strict and (not entry.owner or entry.owner not in opts.allow_proposed_owners):
                severity = SEVERITY_ERROR
            add(
                severity,
                RULE_STATUS,
                "registry entry is proposed and awaits observability governance ratification",
            )
    elif name.startswith("platform_"):
        add(
            SEVERITY_ERROR,
            RULE_NAMESPACE,
            "platform_* metric is not in the Platform Observability Registry; submit it for "
            "ratification",
        )
        return
    else:
        domain = reg.domain_of(name)
        prefix = service_prefix(domain, service)
        if not domain:
            add(
                SEVERITY_ERROR,
                RULE_NAMESPACE,
                "unclassified name; use platform_*, <domain>_* or <domain>_<service>_* with "
                f"domain in {list(reg.domains())}",
            )
            return
        if not service or not name.startswith(prefix):
            add(
                SEVERITY_ERROR,
                RULE_NAMESPACE,
                f"{domain}_* metric is not in the domain registry; service-specific metrics must "
                f"be named {prefix}<metric>",
            )
            return
        tier = TIER_SERVICE
        emitted_domain = family.first_label_value("domain")
        if emitted_domain and emitted_domain != domain:
            add(
                SEVERITY_ERROR,
                RULE_NAMESPACE,
                f"metric is namespaced under domain {domain!r} but emitted with "
                f"domain={emitted_domain!r}",
            )
    report.tiers[name] = tier

    problem = suffix_error(metric_type, name, entry.unit if entry is not None else "")
    if problem:
        add(SEVERITY_ERROR, RULE_SUFFIX, problem)
    _check_labels(reg, family, tier, entry, add)


def _check_labels(
    reg: ObservabilityRegistry,
    family: Family,
    tier: str,
    entry: MetricSpec | None,
    add: Callable[[str, str, str], None],
) -> None:
    required = reg.required_labels.get(tier, ())
    allowed: list[str] | None = None
    if entry is not None:
        allowed = [*reg.const_labels, *entry.labels]
        if entry.tier == TIER_DOMAIN:
            emitted_domain = family.first_label_value("domain")
            if emitted_domain and emitted_domain != entry.domain:
                add(
                    SEVERITY_ERROR,
                    RULE_NAMESPACE,
                    f"domain metric of {entry.domain!r} emitted with domain={emitted_domain!r}",
                )

    reported: set[str] = set()

    def once(key: str, severity: str, rule: str, message: str) -> None:
        if key not in reported:
            reported.add(key)
            add(severity, rule, message)

    distinct: dict[str, set[str]] = defaultdict(set)
    for labels in family.series:
        for label, value in labels.items():
            if reg.is_prohibited_label(label):
                once(
                    "prohibited:" + label,
                    SEVERITY_ERROR,
                    RULE_PROHIBITED_LABEL,
                    f"label {label!r} is prohibited (unbounded cardinality)",
                )
                continue
            if allowed is not None and label not in allowed:
                once(
                    "vocab:" + label,
                    SEVERITY_ERROR,
                    RULE_LABEL_VOCABULARY,
                    f"label {label!r} is not approved for this shared metric (allowed: {allowed})",
                )
                continue
            if label not in reg.labels:
                # Service-owned label on a Tier 3 metric.
                if not LABEL_NAME_RE.fullmatch(label):
                    once(
                        "labelname:" + label,
                        SEVERITY_ERROR,
                        RULE_LABEL_NAME,
                        f"label {label!r} must be snake_case [a-z0-9_]",
                    )
                continue
            if not reg.allows(entry, label, value):
                once(
                    f"value:{label}={value}",
                    SEVERITY_ERROR,
                    RULE_LABEL_VALUE,
                    f"label {label}={value!r} is outside the vocabulary",
                )
            distinct[label].add(value)
        for label in required:
            if label not in labels:
                once(
                    "required:" + label,
                    SEVERITY_ERROR,
                    RULE_REQUIRED_LABEL,
                    f"required label {label!r} is missing",
                )
        if entry is not None:
            for label in entry.labels:
                if label not in labels:
                    once(
                        "declared:" + label,
                        SEVERITY_ERROR,
                        RULE_LABEL_VOCABULARY,
                        f"registry label {label!r} is missing",
                    )
    for label in sorted(distinct):
        limit = reg.labels[label].max_values
        if limit and len(distinct[label]) > limit:
            add(
                SEVERITY_ERROR,
                RULE_CARDINALITY,
                f"label {label!r} has {len(distinct[label])} distinct values (limit {limit})",
            )
