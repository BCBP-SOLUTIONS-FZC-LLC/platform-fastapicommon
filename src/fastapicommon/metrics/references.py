"""Scan dashboards, alert / recording rules, SLOs, HPA manifests and runbooks for metric names
that need attention.

Port of platform-gincommon's ``internal/obsregistry/refs.go`` (``metricslint refs``): a legacy
name is reported with its replacement (an error once removed, a warning while deprecated), and a
``platform_*`` series name without a registry entry is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import IO

from fastapicommon.metrics.registry import (
    HISTOGRAM,
    STATUS_REMOVED,
    TIER_LEGACY,
    MetricSpec,
    ObservabilityRegistry,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: Per-file cap, the same bound as a fetched scrape (64 MiB).
MAX_REFERENCE_FILE_BYTES = 64 << 20

_IDENT_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_HISTOGRAM_SERIES_SUFFIXES = ("_bucket", "_sum", "_count")
_SERIES_SUFFIXES = ("_total", "_seconds", "_bytes", "_bucket", "_sum", "_count")


class ReferenceFileTooLargeError(ValueError):
    """Input beyond :data:`MAX_REFERENCE_FILE_BYTES`."""


@dataclass(frozen=True)
class Reference:
    file: str
    line: int
    metric: str
    severity: str
    replacement: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.severity}: {self.message}"


def scan_references(
    registry: ObservabilityRegistry,
    file: str,
    stream: IO[str],
    *,
    limit: int | None = None,
) -> list[Reference]:
    """The references in ``stream`` (labelled ``file``). Recording-rule names (containing ``:``)
    and prose such as ``platform_*`` are skipped. Raises :class:`ReferenceFileTooLargeError`
    (carrying the references found so far as ``.references``) past ``limit`` bytes (default
    :data:`MAX_REFERENCE_FILE_BYTES`)."""
    if limit is None:
        limit = MAX_REFERENCE_FILE_BYTES
    refs: list[Reference] = []
    total = 0
    for number, text in enumerate(stream, start=1):
        total += len(text.encode())
        if total > limit:
            error = ReferenceFileTooLargeError(
                f"scan {file}: file exceeds the reference scan size limit ({limit} bytes)"
            )
            error.references = refs  # type: ignore[attr-defined]
            raise error
        seen: set[str] = set()
        for token in _IDENT_RE.findall(text):
            if ":" in token or token.endswith("_") or token in seen:
                continue
            seen.add(token)
            ref = _check_reference(registry, token)
            if ref is not None:
                refs.append(
                    Reference(file, number, ref.metric, ref.severity, ref.replacement, ref.message)
                )
    return refs


def _check_reference(registry: ObservabilityRegistry, token: str) -> Reference | None:
    entry, base = _resolve_series(registry, token)
    if entry is not None and entry.tier == TIER_LEGACY:
        replacements = []
        for name in entry.replacements:
            suffix = token.removeprefix(base)
            successor = registry.lookup(name)
            # Carry a histogram series suffix over when the successor is a histogram too.
            replacements.append(
                name + suffix
                if suffix and successor is not None and successor.type == HISTOGRAM
                else name
            )
        replacement = " or ".join(replacements)
        if entry.status == STATUS_REMOVED:
            message = (
                f"legacy metric {token} was removed and is no longer emitted; use {replacement}"
            )
            return Reference("", 0, token, SEVERITY_ERROR, replacement, message)
        message = f"legacy metric {token} is deprecated (sunset: {entry.sunset}); use {replacement}"
        return Reference("", 0, token, SEVERITY_WARNING, replacement, message)
    # Unregistered names are only reported when they look like a series name; group names and
    # prose (platform_events, ...) are not metrics.
    if entry is None and token.startswith("platform_") and token.endswith(_SERIES_SUFFIXES):
        message = f"{token} is not in the Platform Observability Registry"
        return Reference("", 0, token, SEVERITY_ERROR, "", message)
    return None


def _resolve_series(registry: ObservabilityRegistry, token: str) -> tuple[MetricSpec | None, str]:
    entry = registry.lookup(token)
    if entry is not None:
        return entry, token
    for suffix in _HISTOGRAM_SERIES_SUFFIXES:
        base = token.removesuffix(suffix)
        if base != token:
            entry = registry.lookup(base)
            if entry is not None and entry.type == HISTOGRAM:
                return entry, base
    return None, token
