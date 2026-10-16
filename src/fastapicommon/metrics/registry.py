"""Platform Observability Registry.

``registry.json`` next to this module is a vendored, byte-identical copy of
``platform-gincommon``'s ``internal/obsregistry/registry.json`` — the central registry of the
Enterprise Platform Observability Standard. Go is the source of truth: the file is never edited
here, only re-copied when gincommon's registry changes (CI compares it with gincommon at
``GINCOMMON_REF``, and ``tests/unit/metrics/test_metrics_registry.py`` with a sibling checkout
when one is present).

The ``platform_*`` collectors in :mod:`fastapicommon.metrics.platform` take their names, help
text, label names (in order) and buckets from this registry, so the emitted contract cannot drift
from Go's. This module is the Python port of Go's ``internal/obsregistry/registry.go``:
:func:`parse_registry` applies the same self-consistency rules as ``obsregistry.Parse``, and
:meth:`ObservabilityRegistry.check_identity`, :meth:`~ObservabilityRegistry.check_custom`,
:meth:`~ObservabilityRegistry.shared_metric` and :func:`service_prefix` mirror their Go
counterparts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any

REGISTRY_RESOURCE = "registry.json"

#: Registry tiers. ``legacy`` covers pre-standard names (removed or deprecated); ``exempt``
#: classifies runtime collectors (``python_*``, ``process_*``, ...) during validation only.
TIER_PLATFORM = "platform"
TIER_DOMAIN = "domain"
TIER_SERVICE = "service"
TIER_LEGACY = "legacy"
TIER_EXEMPT = "exempt"

#: Registry statuses: ``proposed`` entries await observability governance ratification.
STATUS_APPROVED = "approved"
STATUS_PROPOSED = "proposed"
STATUS_DEPRECATED = "deprecated"
STATUS_REMOVED = "removed"

COUNTER = "counter"
GAUGE = "gauge"
HISTOGRAM = "histogram"

#: snake_case metric and label names (Go ``nameRE`` / ``labelNameRE``).
NAME_RE = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
LABEL_NAME_RE = NAME_RE

_DOCUMENT_KEYS = {
    "schema_version",
    "const_labels",
    "required_labels",
    "prohibited_labels",
    "prohibited_label_suffixes",
    "exempt_prefixes",
    "bucket_presets",
    "labels",
    "metrics",
}
_LABEL_KEYS = {"description", "values", "pattern", "max_values", "cardinality"}
_METRIC_KEYS = {
    "name",
    "tier",
    "domain",
    "owner",
    "type",
    "unit",
    "buckets",
    "status",
    "help",
    "labels",
    "label_values",
    "aggregation",
    "cardinality",
    "governance_notes",
    "replacements",
    "sunset",
}


class RegistryError(ValueError):
    """A registry document that was read but violates the standard (every problem listed)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass(frozen=True)
class LabelSpec:
    """One entry of the registry's label vocabulary: an enum (``values``) or a ``pattern``."""

    name: str
    values: tuple[str, ...]
    pattern: re.Pattern[str] | None
    max_values: int | None
    description: str = ""
    cardinality: str = ""

    def allows(self, value: str) -> bool:
        """Mirror of Go ``Label.Allows`` (RE2 ``MatchString``: an unanchored search; registry
        patterns anchor themselves with ``^...$``, compiled by :func:`_go_regex`)."""
        if self.pattern is not None:
            return self.pattern.search(value) is not None
        return value in self.values


@dataclass(frozen=True)
class MetricSpec:
    """One registry metric entry."""

    name: str
    tier: str
    type: str
    help: str
    labels: tuple[str, ...]
    buckets: str | None
    domain: str | None
    owner: str = ""
    unit: str = ""
    status: str = ""
    label_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    aggregation: str = ""
    cardinality: str = ""
    governance_notes: str = ""
    replacements: tuple[str, ...] = ()
    sunset: str = ""


def _go_regex(pattern: str) -> re.Pattern[str]:
    """Compile an RE2 pattern with Go's ``$`` semantics (end of text, not before a final
    newline)."""
    return re.compile(re.sub(r"(?<!\\)\$", r"\\Z", pattern))


class ObservabilityRegistry:
    """Parsed registry document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.schema_version: int = document.get("schema_version", 0)
        self.const_labels: tuple[str, ...] = tuple(document["const_labels"])
        self.required_labels: dict[str, tuple[str, ...]] = {
            tier: tuple(names) for tier, names in (document.get("required_labels") or {}).items()
        }
        self.prohibited_labels: tuple[str, ...] = tuple(document.get("prohibited_labels") or ())
        self.prohibited_label_suffixes: tuple[str, ...] = tuple(
            document.get("prohibited_label_suffixes") or ()
        )
        self.exempt_prefixes: tuple[str, ...] = tuple(document.get("exempt_prefixes") or ())
        self.bucket_presets: dict[str, tuple[float, ...]] = {
            name: tuple(float(b) for b in buckets)
            for name, buckets in document["bucket_presets"].items()
        }
        self.labels: dict[str, LabelSpec] = {
            name: LabelSpec(
                name=name,
                values=tuple(spec.get("values") or ()),
                pattern=_go_regex(spec["pattern"]) if spec.get("pattern") else None,
                max_values=spec.get("max_values") or None,
                description=spec.get("description", ""),
                cardinality=spec.get("cardinality", ""),
            )
            for name, spec in document["labels"].items()
        }
        self._raw_patterns: dict[str, str] = {
            name: spec["pattern"]
            for name, spec in document["labels"].items()
            if spec.get("pattern")
        }
        self.metrics: dict[str, MetricSpec] = {}
        self.ordered_metrics: list[MetricSpec] = []
        for entry in document["metrics"]:
            spec = MetricSpec(
                name=entry["name"],
                tier=entry["tier"],
                type=entry["type"],
                help=entry.get("help", ""),
                labels=tuple(entry.get("labels") or ()),
                buckets=entry.get("buckets"),
                domain=entry.get("domain"),
                owner=entry.get("owner", ""),
                unit=entry.get("unit", ""),
                status=entry.get("status", ""),
                label_values={
                    label: tuple(values)
                    for label, values in (entry.get("label_values") or {}).items()
                },
                aggregation=entry.get("aggregation", ""),
                cardinality=entry.get("cardinality", ""),
                governance_notes=entry.get("governance_notes", ""),
                replacements=tuple(entry.get("replacements") or ()),
                sunset=entry.get("sunset", ""),
            )
            self.ordered_metrics.append(spec)
            self.metrics.setdefault(spec.name, spec)

    # -- lookups ---------------------------------------------------------------------------------

    def lookup(self, name: str) -> MetricSpec | None:
        return self.metrics.get(name)

    def buckets(self, preset: str) -> tuple[float, ...]:
        return self.bucket_presets[preset]

    def label_pattern(self, label: str) -> str:
        """The label's pattern exactly as written in the document (``""`` for an enum)."""
        return self._raw_patterns.get(label, "")

    def domains(self) -> tuple[str, ...]:
        label = self.labels.get("domain")
        return label.values if label is not None else ()

    def is_domain(self, domain: str) -> bool:
        return domain != "" and domain in self.domains()

    def environments(self) -> tuple[str, ...]:
        label = self.labels.get("environment")
        return label.values if label is not None else ()

    def domain_of(self, name: str) -> str:
        """The approved domain ``name`` is namespaced under (longest match), else ``""``."""
        best = ""
        for domain in self.domains():
            if name.startswith(domain + "_") and len(domain) > len(best):
                best = domain
        return best

    def is_exempt(self, name: str) -> bool:
        return name.startswith(self.exempt_prefixes) if self.exempt_prefixes else False

    def is_prohibited_label(self, name: str) -> bool:
        if name in self.prohibited_labels:
            return True
        return any(name.endswith(suffix) for suffix in self.prohibited_label_suffixes)

    def allows(self, entry: MetricSpec | None, label: str, value: str) -> bool:
        """``value`` is allowed for ``label`` on ``entry``: the entry's own ``label_values`` when
        it declares them, else the label's vocabulary (an unknown label allows anything)."""
        if entry is not None and label in entry.label_values:
            return value in entry.label_values[label]
        spec = self.labels.get(label)
        return spec is None or spec.allows(value)

    # -- identity --------------------------------------------------------------------------------

    def normalize_environment(self, environment: str) -> str:
        """Trim + lowercase ``environment`` and require it to be in the environment vocabulary
        (``local, dev, test, staging, prod``). There are no aliases: ``production`` is rejected,
        as in Go. Raises ``ValueError`` otherwise."""
        normalized = environment.strip().lower()
        label = self.labels.get("environment")
        if label is not None and label.allows(normalized):
            return normalized
        raise ValueError(
            f"environment {environment!r} is not in the registry vocabulary "
            f"{list(self.environments())} (set APP_ENV or PlatformSettings.environment to one "
            "of them)"
        )

    def check_identity(self, domain: str, service: str, environment: str) -> str:
        """Validate the centrally injected identity labels; returns the normalized environment.

        Mirror of Go ``obsregistry.Registry.CheckIdentity``.
        """
        if not self.is_domain(domain):
            raise ValueError(
                f"domain {domain!r} is not in the registry vocabulary {list(self.domains())}"
            )
        service_label = self.labels["service"]
        if not service_label.allows(service):
            raise ValueError(
                f"service {service!r} does not match the service label pattern "
                f"{self.label_pattern('service')}"
            )
        return self.normalize_environment(environment)

    # -- metric construction rules ---------------------------------------------------------------

    def check_custom(
        self,
        metric_type: str,
        name: str,
        labelnames: tuple[str, ...] | list[str] = (),
        domain: str = "",
        service: str = "",
    ) -> None:
        """Validate a service-owned (Tier 3) collector before it is registered; raises
        ``ValueError``. Mirror of Go ``Registry.CheckCustom``: with an empty ``domain`` only the
        naming and label rules apply."""
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"metric {name!r}: name must be snake_case [a-z0-9_]")
        entry = self.lookup(name)
        if entry is not None:
            if entry.tier == TIER_LEGACY:
                raise ValueError(
                    f"metric {name!r} is a {entry.status} legacy name; emit its replacement "
                    f"{', '.join(entry.replacements)} instead"
                )
            raise ValueError(
                f"metric {name!r} is a {entry.tier} registry metric; create it from the registry "
                "(new_shared_*) instead"
            )
        if name.startswith("platform_"):
            raise ValueError(
                f"metric {name!r}: platform_* metrics must be ratified in the Platform "
                "Observability Registry before use"
            )
        if self.is_exempt(name):
            raise ValueError(f"metric {name!r}: prefix is reserved for runtime collectors")
        problem = suffix_error(metric_type, name, "")
        if problem:
            raise ValueError(f"metric {name!r}: {problem}")
        for label in labelnames:
            if not LABEL_NAME_RE.fullmatch(label):
                raise ValueError(f"metric {name!r}: label {label!r} must be snake_case")
            if self.is_prohibited_label(label):
                raise ValueError(
                    f"metric {name!r}: label {label!r} is prohibited (unbounded cardinality)"
                )
            if label in self.const_labels:
                raise ValueError(
                    f"metric {name!r}: label {label!r} is injected centrally and must not be "
                    "declared"
                )
        if domain:
            prefix = service_prefix(domain, service)
            if not name.startswith(prefix):
                raise ValueError(
                    f"metric {name!r}: service-specific metrics must be named {prefix}<metric>; "
                    f"domain-shared {domain}_* metrics must be ratified in the registry"
                )

    def shared_metric(self, name: str, metric_type: str, domain: str) -> MetricSpec:
        """The registry entry a shared (Tier 1/2) collector named ``name`` is built from, checking
        a service in ``domain`` may emit it; raises ``ValueError``. Mirror of Go
        ``Registry.SharedMetric``."""
        entry = self.lookup(name)
        if entry is None:
            raise ValueError(f"metric {name!r} is not in the Platform Observability Registry")
        if entry.tier not in (TIER_PLATFORM, TIER_DOMAIN):
            raise ValueError(f"metric {name!r} is a {entry.tier} metric, not a shared one")
        if entry.type != metric_type:
            raise ValueError(f"metric {name!r} is a {entry.type}, not a {metric_type}")
        if entry.tier == TIER_DOMAIN and entry.domain != domain:
            raise ValueError(
                f"metric {name!r} belongs to domain {entry.domain!r}; this service is in domain "
                f"{domain!r}"
            )
        return entry


def suffix_error(metric_type: str, name: str, unit: str) -> str:
    """The standard's suffix rules (Go ``suffixError``); ``""`` when ``name`` complies:
    counters end in ``_total``, histograms in ``_seconds`` (``_bytes`` for registry entries
    declaring ``unit: bytes``), and gauges never in ``_total``."""
    if metric_type == COUNTER:
        return "" if name.endswith("_total") else "counters must end in _total"
    if metric_type == HISTOGRAM:
        if unit not in ("", "seconds", "bytes"):
            return f"histogram unit {unit!r} is not supported (want seconds or bytes)"
        want = "_bytes" if unit == "bytes" else "_seconds"
        return "" if name.endswith(want) else f"histograms must end in {want}"
    if metric_type == GAUGE:
        if name.endswith("_total"):
            return "gauges must not end in _total (reserved for counters)"
        return ""
    return f"unknown type {metric_type!r}"


def service_prefix(domain: str, service: str) -> str:
    """The ``<domain>_<service>_`` prefix Tier 3 metrics of ``service`` must use (Go
    ``ServicePrefix``). A service name already starting with the domain (``iam-event-consumer``
    in ``iam``) is not prefixed twice."""
    normalized = "".join(
        c if ("a" <= c <= "z" or "0" <= c <= "9") else "_" for c in service.lower()
    )
    normalized = normalized.strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    normalized = normalized.removeprefix(domain + "_")
    return f"{domain}_{normalized}_"


# --------------------------------------------------------------------------------------------
# Strict parsing (Go obsregistry.Parse)
# --------------------------------------------------------------------------------------------


def parse_registry(raw: bytes | str) -> ObservabilityRegistry:
    """Decode a registry document and check it against the standard's naming, labelling and
    governance rules (Go ``obsregistry.Parse``). Raises :class:`RegistryError` listing every
    problem, or ``ValueError`` when the document is not valid JSON of the right shape."""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"decode registry: {exc}") from exc
    problems = _structure_problems(document)
    if problems:
        raise RegistryError(problems)
    registry = ObservabilityRegistry(document)
    problems = _compile_problems(registry, document)
    if problems:
        raise RegistryError(problems)
    return registry


def _structure_problems(document: Any) -> list[str]:
    """Unknown fields and missing sections (Go's ``DisallowUnknownFields`` decode)."""
    if not isinstance(document, dict):
        return ["decode registry: the document must be a JSON object"]
    problems = [
        f"decode registry: unknown field {key!r}" for key in document.keys() - _DOCUMENT_KEYS
    ]
    for key in ("const_labels", "bucket_presets", "labels", "metrics"):
        if key not in document:
            problems.append(f"decode registry: {key} is required")
    for name, spec in (document.get("labels") or {}).items():
        for key in spec.keys() - _LABEL_KEYS:
            problems.append(f"decode registry: label {name!r}: unknown field {key!r}")
    for entry in document.get("metrics") or []:
        for key in entry.keys() - _METRIC_KEYS:
            problems.append(f"decode registry: metric {entry.get('name')!r}: unknown field {key!r}")
        for key in ("name", "tier", "type"):
            if key not in entry:
                problems.append(f"decode registry: metric {entry.get('name')!r}: {key} is required")
    return problems


def _compile_problems(registry: ObservabilityRegistry, document: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    fail = problems.append

    if registry.schema_version != 1:
        fail(f"schema_version {registry.schema_version} is not supported (want 1)")
    for name in sorted(document["labels"]):
        spec = document["labels"][name]
        if not LABEL_NAME_RE.fullmatch(name):
            fail(f"label {name!r}: name is not snake_case")
        if registry.is_prohibited_label(name):
            fail(f"label {name!r}: name is prohibited (high cardinality)")
        if not spec.get("description") or not spec.get("cardinality"):
            fail(f"label {name!r}: description and cardinality are required")
        values, pattern = spec.get("values"), spec.get("pattern")
        if values and pattern:
            fail(f"label {name!r}: set values or pattern, not both")
        elif not values and not pattern:
            fail(f"label {name!r}: values or pattern is required")
    for name in registry.const_labels:
        if name not in registry.labels:
            fail(f"const label {name!r} is not defined in labels")
    for name in ("domain", "service", "environment"):
        if name not in registry.const_labels:
            fail(f"const_labels must include {name!r}")
    for tier in (TIER_PLATFORM, TIER_DOMAIN, TIER_SERVICE):
        if tier not in registry.required_labels:
            fail(f"required_labels.{tier} is missing")
        for name in registry.required_labels.get(tier, ()):
            if name not in registry.const_labels:
                fail(
                    f"required label {name!r} ({tier}) must be a const label so it is injected "
                    "centrally"
                )

    seen: set[str] = set()
    for entry in registry.ordered_metrics:
        if entry.name in seen:
            fail(f"metric {entry.name!r}: duplicate entry")
        seen.add(entry.name)
    for entry in registry.ordered_metrics:
        problems.extend(f"metric {entry.name!r}: {p}" for p in _entry_problems(registry, entry))
    return problems


def _entry_problems(registry: ObservabilityRegistry, m: MetricSpec) -> list[str]:
    problems: list[str] = []
    fail = problems.append
    if not NAME_RE.fullmatch(m.name):
        fail("name is not snake_case")
    if not m.help:
        fail("help is required")
    # Legacy entries record pre-standard names as they are; naming rules apply to their
    # replacements.
    suffix = suffix_error(m.type, m.name, m.unit)
    if suffix and m.tier != TIER_LEGACY:
        fail(suffix)
    if m.type == HISTOGRAM and m.buckets and m.buckets not in registry.bucket_presets:
        fail(f"unknown bucket preset {m.buckets!r}")
    if m.status not in (STATUS_APPROVED, STATUS_PROPOSED, STATUS_DEPRECATED, STATUS_REMOVED):
        fail(f"unknown status {m.status!r}")

    if m.tier in (TIER_PLATFORM, TIER_DOMAIN):
        if m.status in (STATUS_DEPRECATED, STATUS_REMOVED):
            fail(f"shared metrics are retired by deleting the entry, not by status {m.status}")
        if not m.aggregation:
            fail("aggregation expectations are required for shared metrics")
        if m.tier == TIER_PLATFORM and not m.name.startswith("platform_"):
            fail("platform-tier metrics must be named platform_*")
        if m.tier == TIER_DOMAIN:
            if not registry.is_domain(m.domain or ""):
                fail(f"unknown domain {m.domain or ''!r}")
            elif not m.name.startswith(f"{m.domain}_"):
                fail(f"domain-tier metrics must be named {m.domain}_*")
        if m.type == HISTOGRAM and not m.buckets:
            fail("shared histograms must name a bucket preset so buckets aggregate across services")
        for label in m.labels:
            if label not in registry.labels:
                fail(f"label {label!r} is not in the label vocabulary")
            elif label in registry.const_labels:
                fail(f"label {label!r} is a const label and is injected centrally")
        for label in sorted(m.label_values):
            if label not in m.labels:
                fail(f"label_values for {label!r}, which is not one of its labels")
                continue
            spec = registry.labels.get(label)
            for value in m.label_values[label]:
                if spec is not None and not spec.allows(value):
                    fail(f"label_values {label}={value!r} is outside the label vocabulary")
    elif m.tier == TIER_LEGACY:
        if m.status not in (STATUS_DEPRECATED, STATUS_REMOVED):
            fail("legacy metrics must be deprecated or removed")
        if not m.replacements:
            fail("replacements are required for legacy metrics")
        for name in m.replacements:
            replacement = registry.lookup(name)
            if replacement is None:
                fail(f"replacement {name!r} is not in the registry")
            elif replacement.tier == TIER_LEGACY:
                fail(f"replacement {name!r} is itself legacy")
        if not m.sunset:
            fail("sunset is required for legacy metrics")
    else:
        fail(f"tier {m.tier!r} cannot be registered (want platform, domain or legacy)")
    return problems


# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------


def load_registry(raw: bytes | str) -> ObservabilityRegistry:
    """Parse a registry document without the self-consistency checks (e.g. a test fixture);
    :func:`parse_registry` is the strict variant."""
    return ObservabilityRegistry(json.loads(raw))


def registry_bytes() -> bytes:
    """The vendored registry document, exactly as shipped in the package."""
    return resources.files("fastapicommon.metrics").joinpath(REGISTRY_RESOURCE).read_bytes()


@cache
def default_registry() -> ObservabilityRegistry:
    """The registry vendored into this package version, strictly parsed once per process (a
    failure is a packaging defect, as Go's ``Default()`` panics)."""
    return parse_registry(registry_bytes())
