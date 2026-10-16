"""``fastapicommon-metricslint``: the Enterprise Platform Observability Standard on the command
line (Python port of platform-gincommon's ``cmd/metricslint``, same commands and exit codes).

Usage::

  fastapicommon-metricslint check [--strict] [--allow-proposed-owner LIST] [--registry FILE]
                                  [--url URL | FILE ...]
  fastapicommon-metricslint refs [--deny-deprecated] [--ext LIST] [--registry FILE] PATH ...
  fastapicommon-metricslint registry [--registry FILE]
  fastapicommon-metricslint inventory [--registry FILE] [-o FILE]

Exit codes: 0 compliant, 1 findings (for ``registry``: the document violates the standard),
2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from fastapicommon.metrics.compliance import Options, validate_text
from fastapicommon.metrics.inventory import render_inventory
from fastapicommon.metrics.references import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Reference,
    ReferenceFileTooLargeError,
    scan_references,
)
from fastapicommon.metrics.registry import (
    ObservabilityRegistry,
    RegistryError,
    default_registry,
    parse_registry,
)

DEFAULT_REF_EXTENSIONS = ".yml,.yaml,.json,.jsonnet,.libsonnet,.rules,.promql,.tf"
#: Directories ``refs`` never descends into (VCS metadata, vendored third-party trees).
SKIP_DIRS = {".git", "node_modules", "vendor"}
MAX_SCRAPE_BYTES = 64 << 20


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="fastapicommon-metricslint",
        description="Enforce the Enterprise Platform Observability Standard.",
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)

    check = commands.add_parser("check", help="validate a Prometheus / OpenMetrics scrape")
    check.add_argument("--strict", action="store_true", help="proposed registry metrics are errors")
    check.add_argument(
        "--allow-proposed-owner",
        default="",
        help="with --strict, comma-separated registry owners whose proposed metrics stay warnings",
    )
    check.add_argument("--url", default="", help="scrape this /metrics URL instead of files")
    check.add_argument("files", nargs="*", help="scrape files (default: stdin)")

    refs = commands.add_parser("refs", help="scan dashboards / rules / manifests for metric names")
    refs.add_argument("--deny-deprecated", action="store_true", help="fail on deprecated names")
    refs.add_argument("--ext", default=DEFAULT_REF_EXTENSIONS, help="extensions scanned in dirs")
    refs.add_argument("paths", nargs="+")

    commands.add_parser("registry", help="validate a registry document")

    inventory = commands.add_parser("inventory", help="render the Markdown inventory")
    inventory.add_argument("-o", dest="output", default="", help="write to this file")

    for sub in (check, refs, commands.choices["registry"], inventory):
        sub.add_argument("--registry", default="", help="registry JSON (default: the vendored one)")
    return parser


def main(argv: Sequence[str] | None = None, stdin: IO[str] | None = None) -> int:
    stdout, stderr = sys.stdout, sys.stderr
    try:
        args = _parser().parse_args(argv)
    except _UsageError as exc:
        print(f"metricslint: {exc}\n\n{__doc__}", file=stderr)
        return 2
    try:
        registry = _load_registry(args.registry)
    except RegistryError as exc:
        print(f"metricslint: {exc}", file=stderr)
        return 1 if args.command == "registry" else 2
    except (OSError, ValueError) as exc:
        print(f"metricslint: {exc}", file=stderr)
        return 2

    if args.command == "registry":
        print(
            f"registry OK: {len(registry.ordered_metrics)} metrics, {len(registry.labels)} labels, "
            f"{len(registry.domains())} domains",
            file=stdout,
        )
        return 0
    if args.command == "inventory":
        text = render_inventory(registry)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            stdout.write(text)
        return 0
    if args.command == "check":
        return _check(registry, args, stdin or sys.stdin, stdout, stderr)
    return _refs(registry, args.paths, args.ext.split(","), args.deny_deprecated, stdout, stderr)


def _load_registry(path: str) -> ObservabilityRegistry:
    if not path:
        return default_registry()
    return parse_registry(Path(path).read_bytes())


def _check(
    registry: ObservabilityRegistry,
    args: argparse.Namespace,
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    if args.url and args.files:
        print("metricslint: check: --url and files are mutually exclusive", file=stderr)
        return 2
    options = Options(
        strict=args.strict,
        allow_proposed_owners=tuple(
            item.strip() for item in args.allow_proposed_owner.split(",") if item.strip()
        ),
    )
    try:
        if args.url:
            inputs = [_fetch(args.url)]
        elif args.files:
            inputs = [Path(f).read_text(encoding="utf-8") for f in args.files]
        else:
            inputs = [stdin.read()]
        code = 0
        for text in inputs:
            report = validate_text(text, options, observability_registry=registry)
            stdout.write(str(report))
            if not report.ok():
                code = 1
        return code
    except (OSError, ValueError) as exc:
        print(f"metricslint: {exc}", file=stderr)
        return 2


def _fetch(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"Accept": "text/plain;version=0.0.4;q=1,application/openmetrics-text;q=0.5"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - operator URL
        body: bytes = response.read(MAX_SCRAPE_BYTES + 1)
    if len(body) > MAX_SCRAPE_BYTES:
        raise ValueError(f"GET {url}: response exceeds {MAX_SCRAPE_BYTES} bytes")
    return body.decode("utf-8")


def _refs(
    registry: ObservabilityRegistry,
    paths: Sequence[str],
    extensions: Sequence[str],
    deny_deprecated: bool,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    refs: list[Reference] = []
    io_errors = 0

    def unreadable(path: str, error: Exception) -> None:
        nonlocal io_errors
        io_errors += 1
        refs.append(Reference(path, 0, "", SEVERITY_ERROR, "", f"cannot read: {error}"))

    def scan(path: str) -> None:
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                refs.extend(scan_references(registry, path, stream))
        except ReferenceFileTooLargeError as exc:
            refs.extend(getattr(exc, "references", []))
            unreadable(path, exc)
        except OSError as exc:
            unreadable(path, exc)

    for root in paths:
        if not os.path.exists(root):
            unreadable(root, FileNotFoundError(f"no such file or directory: {root}"))
            continue
        if not os.path.isdir(root):
            scan(root)
            continue
        for directory, dirnames, filenames in os.walk(
            root, onerror=lambda exc: unreadable(str(exc.filename), exc)
        ):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for filename in sorted(filenames):
                if os.path.splitext(filename)[1] in extensions:
                    scan(os.path.join(directory, filename))

    errors = warnings = 0
    for ref in refs:
        severity = ref.severity
        if severity == SEVERITY_WARNING and deny_deprecated:
            severity = SEVERITY_ERROR
        if severity == SEVERITY_ERROR:
            errors += 1
        else:
            warnings += 1
        print(f"{ref.file}:{ref.line}: {severity}: {ref.message}", file=stdout)
    print(f"{len(refs)} references: {errors} errors, {warnings} warnings", file=stdout)
    if io_errors:
        print(f"metricslint: refs: {io_errors} path(s) could not be read", file=stderr)
        return 2
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
