"""Platform Observability Registry (read-only view).

``registry.json`` next to this module is a vendored, byte-identical copy of
``platform-gincommon``'s ``internal/obsregistry/registry.json`` — the central registry of the
Enterprise Platform Observability Standard. Go is the source of truth: the file is never edited
here, only re-copied when gincommon's registry changes
(``tests/unit/metrics/test_metrics_registry.py`` fails on drift when the sibling checkout is
present).

The ``platform_*`` collectors in :mod:`fastapicommon.metrics.platform` take their names, help
text, label names (in order) and buckets from this registry, so the emitted contract cannot drift
from Go's. The identity checks below mirror ``obsregistry.Registry.CheckIdentity`` /
``NormalizeEnvironment``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

REGISTRY_RESOURCE = "registry.json"


@dataclass(frozen=True)
class LabelSpec:
    """One entry of the registry's label vocabulary: an enum (``values``) or a ``pattern``."""

    name: str
    values: tuple[str, ...]
    pattern: re.Pattern[str] | None
    max_values: int | None

    def allows(self, value: str) -> bool:
        """Mirror of Go ``Label.Allows``. ``re.fullmatch`` keeps RE2's ``$`` semantics (Python's
        ``$`` would otherwise also match before a trailing newline)."""
        if self.pattern is not None:
            return self.pattern.fullmatch(value) is not None
        return value in self.values


@dataclass(frozen=True)
class MetricSpec:
    """One registry metric entry (only the fields this library consumes)."""

    name: str
    tier: str
    type: str
    help: str
    labels: tuple[str, ...]
    buckets: str | None
    domain: str | None
    replacement: str | None


class ObservabilityRegistry:
    """Parsed registry document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.const_labels: tuple[str, ...] = tuple(document["const_labels"])
        self.bucket_presets: dict[str, tuple[float, ...]] = {
            name: tuple(float(b) for b in buckets)
            for name, buckets in document["bucket_presets"].items()
        }
        self.labels: dict[str, LabelSpec] = {
            name: LabelSpec(
                name=name,
                values=tuple(spec.get("values") or ()),
                pattern=re.compile(spec["pattern"]) if spec.get("pattern") else None,
                max_values=spec.get("max_values"),
            )
            for name, spec in document["labels"].items()
        }
        self.metrics: dict[str, MetricSpec] = {
            entry["name"]: MetricSpec(
                name=entry["name"],
                tier=entry["tier"],
                type=entry["type"],
                help=entry["help"],
                labels=tuple(entry.get("labels") or ()),
                buckets=entry.get("buckets"),
                domain=entry.get("domain"),
                replacement=entry.get("replacement"),
            )
            for entry in document["metrics"]
        }

    def lookup(self, name: str) -> MetricSpec | None:
        return self.metrics.get(name)

    def buckets(self, preset: str) -> tuple[float, ...]:
        return self.bucket_presets[preset]

    def domains(self) -> tuple[str, ...]:
        label = self.labels.get("domain")
        return label.values if label is not None else ()

    def is_domain(self, domain: str) -> bool:
        return domain != "" and domain in self.domains()

    def environments(self) -> tuple[str, ...]:
        label = self.labels.get("environment")
        return label.values if label is not None else ()

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
            pattern = service_label.pattern.pattern if service_label.pattern else ""
            raise ValueError(
                f"service {service!r} does not match the service label pattern {pattern}"
            )
        return self.normalize_environment(environment)


def load_registry(raw: bytes | str) -> ObservabilityRegistry:
    """Parse a registry document (e.g. a fixture) into an :class:`ObservabilityRegistry`."""
    return ObservabilityRegistry(json.loads(raw))


def registry_bytes() -> bytes:
    """The vendored registry document, exactly as shipped in the package."""
    return resources.files("fastapicommon.metrics").joinpath(REGISTRY_RESOURCE).read_bytes()


@cache
def default_registry() -> ObservabilityRegistry:
    """The registry vendored into this package version (parsed once per process)."""
    return load_registry(registry_bytes())
