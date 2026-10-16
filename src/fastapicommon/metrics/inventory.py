"""Render the registry as the Markdown observability inventory
(``docs/observability/metric-registry.md``).

Port of platform-gincommon's ``internal/obsregistry/inventory.go``: for the same
``registry.json`` the output is byte-identical to gincommon's ``metric-registry.md`` except for
the source path in the generated-file comment (CI checks this).
"""

from __future__ import annotations

from decimal import Decimal

from fastapicommon.metrics.registry import (
    TIER_DOMAIN,
    TIER_LEGACY,
    TIER_PLATFORM,
    TIER_SERVICE,
    MetricSpec,
    ObservabilityRegistry,
)

SOURCE_PATH = "src/fastapicommon/metrics/registry.json"


def render_inventory(registry: ObservabilityRegistry, source: str = SOURCE_PATH) -> str:
    out: list[str] = [
        "# Platform Observability Registry\n\n",
        f"<!-- Generated from {source} by `make metrics-inventory`. Do not edit. -->\n\n",
        "Every metric carries the centrally injected const labels ",
        _code_list(registry.const_labels),
        ". Required labels: platform tier ",
        _code_list(registry.required_labels.get(TIER_PLATFORM, ())),
        ", domain tier ",
        _code_list(registry.required_labels.get(TIER_DOMAIN, ())),
        ", service tier ",
        _code_list(registry.required_labels.get(TIER_SERVICE, ())),
        ".\n\n",
    ]
    sections = (
        ("Tier 1: platform-shared metrics", TIER_PLATFORM),
        ("Tier 2: domain-shared metrics", TIER_DOMAIN),
        ("Legacy metrics (removed or deprecated)", TIER_LEGACY),
    )
    for title, tier in sections:
        out.append(f"## {title}\n\n")
        if tier == TIER_LEGACY:
            out.append(
                "| Metric | Owner | Type | Status | Replacements | Sunset |\n"
                "|---|---|---|---|---|---|\n"
            )
        else:
            out.append(
                "| Metric | Owner | Type | Status | Labels | Meaning | Aggregation |\n"
                "|---|---|---|---|---|---|---|\n"
            )
        for m in registry.ordered_metrics:
            if m.tier != tier:
                continue
            if tier == TIER_LEGACY:
                out.append(
                    f"| `{m.name}` | {_owner(m)} | {m.type} | {m.status} | "
                    f"{_code_list(m.replacements)} | {_cell(m.sunset)} |\n"
                )
                continue
            typ = m.type + (f" ({m.buckets})" if m.buckets else "")
            name = f"`{m.name}`" + (f" ({m.domain})" if m.domain else "")
            labels = _code_list(m.labels)
            for label in sorted(m.label_values):
                labels += f"<br>`{label}` ∈ " + _code_list(m.label_values[label])
            out.append(
                f"| {name} | {_owner(m)} | {typ} | {m.status} | {labels} | {_cell(m.help)} | "
                f"{_cell(m.aggregation)} |\n"
            )
        out.append("\n")

    out.append(
        "## Label vocabulary\n\n"
        "| Label | Allowed values | Max distinct values | Cardinality | Meaning |\n"
        "|---|---|---|---|---|\n"
    )
    for name in sorted(registry.labels):
        spec = registry.labels[name]
        pattern = registry.label_pattern(name)
        allowed = f"pattern `{pattern}`" if pattern else _code_list(spec.values)
        limit = str(spec.max_values) if spec.max_values else "-"
        out.append(
            f"| `{name}` | {_cell(allowed)} | {limit} | {_cell(spec.cardinality)} | "
            f"{_cell(spec.description)} |\n"
        )

    out.append("\n## Prohibited labels\n\n")
    out.append(_code_list(registry.prohibited_labels))
    out.append(", and any label ending in ")
    out.append(_code_list(registry.prohibited_label_suffixes))
    out.append(".\n\n## Histogram bucket presets\n\n| Preset | Upper bounds |\n|---|---|\n")
    for name in sorted(registry.bucket_presets):
        bounds = " ".join(go_float(b) for b in registry.bucket_presets[name])
        out.append(f"| `{name}` | {bounds} |\n")
    return "".join(out)


def go_float(value: float) -> str:
    """Format a float as Go's ``fmt.Sprint`` does: ``%g`` with the shortest representation,
    exponent form when the decimal exponent is < -4 or >= 6 (``1048576`` → ``1.048576e+06``)."""
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits_tuple = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in digits_tuple.digits)
    nd = len(digits)
    exponent = int(digits_tuple.exponent)
    dp = nd + exponent  # position of the decimal point relative to the digits
    eprec = 6
    if eprec > nd and nd >= dp:
        eprec = nd
    x = dp - 1
    if x < -4 or x >= eprec:
        mantissa = digits[0] + ("." + digits[1:] if nd > 1 else "")
        return f"{sign}{mantissa}e{'+' if x >= 0 else '-'}{abs(x):02d}"
    if dp <= 0:
        return f"{sign}0.{'0' * -dp}{digits}"
    if dp >= nd:
        return f"{sign}{digits}{'0' * (dp - nd)}"
    return f"{sign}{digits[:dp]}.{digits[dp:]}"


def _owner(m: MetricSpec) -> str:
    return m.owner or "services"


def _code_list(items: tuple[str, ...] | list[str]) -> str:
    if not items:
        return "-"
    return "`" + "`, `".join(items) + "`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")
