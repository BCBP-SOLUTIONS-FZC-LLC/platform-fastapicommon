"""Inventory rendering, reference scanning and the ``fastapicommon-metricslint`` CLI (Go
``inventory.go``, ``refs.go``, ``cli.go``)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from fastapicommon.metrics import lint, references
from fastapicommon.metrics.inventory import go_float, render_inventory
from fastapicommon.metrics.references import (
    ReferenceFileTooLargeError,
    scan_references,
)
from fastapicommon.metrics.registry import default_registry, registry_bytes

ROOT = Path(__file__).resolve().parents[3]
#: The Go source of truth, when the workspace's sibling checkout is present.
_GINCOMMON = ROOT.parents[1] / "platform-gincommon"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.0, "0"),
        (0.001, "0.001"),
        (1.0, "1"),
        (262144.0, "262144"),
        (1048576.0, "1.048576e+06"),
        (1e6, "1e+06"),
        (0.00001, "1e-05"),
        (-2.5, "-2.5"),
        (123.456, "123.456"),
        (0.0001, "0.0001"),
    ],
)
def test_go_float(value: float, text: str) -> None:
    assert go_float(value) == text


def test_checked_in_inventory_is_current() -> None:
    inventory = (ROOT / "docs/observability/metric-registry.md").read_text(encoding="utf-8")
    assert render_inventory(default_registry()) == inventory


@pytest.mark.skipif(not _GINCOMMON.exists(), reason="platform-gincommon not checked out")
def test_inventory_matches_gincommon() -> None:
    go = (_GINCOMMON / "docs/observability/metric-registry.md").read_text(encoding="utf-8")
    assert render_inventory(default_registry(), "internal/obsregistry/registry.json") == go


def test_scan_references() -> None:
    text = io.StringIO(
        "expr: sum(rate(http_requests_total[5m]))\n"
        "expr: histogram_quantile(0.9, rate(http_request_duration_seconds_bucket[5m]))\n"
        "expr: rate(platform_http_requests_total[5m]) + rate(platform_ghost_total[1m])\n"
        "record: service:platform_http_error_ratio:rate5m  # platform_* prose platform_events\n"
        "repeat: http_requests_total http_requests_total\n"
    )
    refs = scan_references(default_registry(), "rules.yml", text)
    assert [(r.line, r.metric, r.severity) for r in refs] == [
        (1, "http_requests_total", "error"),
        (2, "http_request_duration_seconds_bucket", "error"),
        (3, "platform_ghost_total", "error"),
        (5, "http_requests_total", "error"),
    ]
    assert refs[1].replacement == "platform_http_request_duration_seconds_bucket"
    assert str(refs[0]).startswith(
        "rules.yml:1: error: legacy metric http_requests_total was removed"
    )


def test_scan_references_deprecated_and_size_limit() -> None:
    from fastapicommon.metrics.registry import load_registry

    document = json.loads(registry_bytes())
    for entry in document["metrics"]:
        if entry["name"] == "http_requests_total":
            entry["status"] = "deprecated"
    registry = load_registry(json.dumps(document))
    (ref,) = scan_references(registry, "d.json", io.StringIO('"http_requests_total"'))
    assert ref.severity == "warning" and "deprecated" in ref.message

    with pytest.raises(ReferenceFileTooLargeError) as info:
        scan_references(
            default_registry(), "big", io.StringIO("http_requests_total\n" * 10), limit=30
        )
    assert len(info.value.references) == 1  # type: ignore[attr-defined]


def _run(
    args: list[str], capsys: pytest.CaptureFixture[str], stdin: str = ""
) -> tuple[int, str, str]:
    code = lint.main(args, stdin=io.StringIO(stdin))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_registry_and_inventory(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _ = _run(["registry"], capsys)
    assert code == 0 and out.startswith("registry OK:")

    broken = tmp_path / "registry.json"
    document = json.loads(registry_bytes())
    document["schema_version"] = 9
    broken.write_text(json.dumps(document))
    assert _run(["registry", "--registry", str(broken)], capsys)[0] == 1
    assert _run(["check", "--registry", str(broken)], capsys)[0] == 2
    assert _run(["registry", "--registry", str(tmp_path / "missing.json")], capsys)[0] == 2

    code, out, _ = _run(["inventory"], capsys)
    assert code == 0 and out.startswith("# Platform Observability Registry")
    target = tmp_path / "inventory.md"
    assert _run(["inventory", "-o", str(target)], capsys)[0] == 0
    assert target.read_text(encoding="utf-8") == out


def test_cli_check(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    good = 'platform_build_info{domain="iam",environment="prod",service="s",version="v"} 1\n'
    scrape = f"# TYPE platform_build_info gauge\n{good}"
    code, out, _ = _run(["check"], capsys, stdin=scrape)
    assert code == 0 and "1 metrics checked: 0 errors, 1 warnings" in out
    assert _run(["check", "--strict"], capsys, stdin=scrape)[0] == 1
    owners = ["check", "--strict", "--allow-proposed-owner", " platform-gincommon , "]
    assert _run(owners, capsys, stdin=scrape)[0] == 0

    bad = tmp_path / "bad.prom"
    bad.write_text("# TYPE stray_total counter\nstray_total 1\n")
    assert _run(["check", str(bad)], capsys)[0] == 1
    assert _run(["check", str(tmp_path / "missing.prom")], capsys)[0] == 2
    assert _run(["check"], capsys, stdin="not a sample")[0] == 2
    assert _run(["check", "--url", "http://x", str(bad)], capsys)[0] == 2
    assert _run(["check", "--url", "http://127.0.0.1:9/metrics"], capsys)[0] == 2


def test_cli_check_url(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

    body = b"# TYPE stray_total counter\nstray_total 1\n"
    monkeypatch.setattr(lint.urllib.request, "urlopen", lambda request, timeout: Response(body))
    assert _run(["check", "--url", "http://svc/metrics"], capsys)[0] == 1
    monkeypatch.setattr(lint, "MAX_SCRAPE_BYTES", 4)
    code, _, err = _run(["check", "--url", "http://svc/metrics"], capsys)
    assert code == 2 and "exceeds" in err


def test_cli_refs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules/clean.yml").write_text("expr: rate(platform_http_requests_total[5m])\n")
    assert _run(["refs", str(tmp_path / "rules")], capsys)[0] == 0

    (tmp_path / "rules/legacy.yaml").write_text("expr: rate(http_requests_total[5m])\n")
    (tmp_path / "rules/notes.txt").write_text("http_requests_total\n")  # not a scanned extension
    (tmp_path / "rules/.git").mkdir()
    (tmp_path / "rules/.git/x.yml").write_text("http_requests_total\n")
    code, out, _ = _run(["refs", str(tmp_path / "rules")], capsys)
    assert code == 1 and "1 references: 1 errors, 0 warnings" in out

    single = tmp_path / "single.md"
    single.write_text("platform_ghost_total\n")
    assert _run(["refs", str(single)], capsys)[0] == 1
    code, out, err = _run(["refs", str(tmp_path / "nope")], capsys)
    assert code == 2 and "cannot read" in out and "could not be read" in err

    huge = tmp_path / "huge.yml"
    huge.write_text("x" * 100)
    monkeypatch.setattr(references, "MAX_REFERENCE_FILE_BYTES", 10)
    assert _run(["refs", str(huge)], capsys)[0] == 2
    monkeypatch.undo()

    unreadable = tmp_path / "locked.yml"
    unreadable.write_text("x")
    unreadable.chmod(0)
    try:
        if not os.access(unreadable, os.R_OK):  # not when running as root
            code, out, _ = _run(["refs", str(unreadable)], capsys)
            assert code == 2 and "cannot read" in out
    finally:
        unreadable.chmod(0o644)


def test_cli_refs_deny_deprecated(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    document = json.loads(registry_bytes())
    for entry in document["metrics"]:
        if entry["name"] == "http_requests_total":
            entry["status"] = "deprecated"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(document))
    rules = tmp_path / "r.yml"
    rules.write_text("http_requests_total\n")
    assert _run(["refs", "--registry", str(registry), str(rules)], capsys)[0] == 0
    deny = ["refs", "--deny-deprecated", "--registry", str(registry), str(rules)]
    assert _run(deny, capsys)[0] == 1


def test_cli_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run([], capsys)[0] == 2
    assert _run(["frobnicate"], capsys)[0] == 2
    assert _run(["refs"], capsys)[0] == 2
