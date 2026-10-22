"""RFC 9457 §3.2 extension members — the port of gincommon v2.1.0's
``test/unit/problem/extensions_test.go``. The expected JSON strings are gincommon's, byte for
byte, rendered the way ``JSONResponse`` renders a problem."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fastapicommon.problem import (
    TYPE_CONFLICT,
    TYPE_INTERNAL_ERROR,
    TYPE_NOT_FOUND,
    TYPE_SERVICE_UNAVAILABLE,
    Problem,
    ProblemType,
    build_problem,
    parse_problem,
    problem_from_response,
    valid_extension_name,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: gincommon's extensions.go, when the workspace's sibling checkout is present.
_GINCOMMON_EXTENSIONS_GO = (
    Path(__file__).resolve().parents[5]
    / "platform-gincommon"
    / "internal"
    / "problem"
    / "extensions.go"
)


def _render(body: dict[str, Any]) -> str:
    """The body as ``JSONResponse`` (Starlette) renders it."""
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _standard_members() -> list[str]:
    """The JSON names of the standard members: every Problem field but extensions."""
    names = {
        "trace_id": "traceId",
        "error_code": "errorCode",
        "tenant_id": "tenantId",
        "validation_errors": "validationErrors",
    }
    return [names.get(f.name, f.name) for f in fields(Problem) if f.name != "extensions"]


def test_valid_extension_name() -> None:
    for name in ("record_version", "activeWorkflows", "abc", "A1_", "retry_after_seconds"):
        assert valid_extension_name(name), name
    for name in ("", "ab", "1abc", "_abc", "record-version", "a.b.c", "naïve", "has space"):
        assert not valid_extension_name(name), name
    assert not valid_extension_name(7)  # type: ignore[arg-type]
    # Every standard member is reserved, in any letter case.
    for name in _standard_members():
        for n in (name, name.upper(), name[:1].upper() + name[1:]):
            assert not valid_extension_name(n), n


@pytest.mark.skipif(
    not _GINCOMMON_EXTENSIONS_GO.exists(), reason="platform-gincommon v2.1+ not checked out"
)
def test_reserved_names_match_gincommon() -> None:
    src = _GINCOMMON_EXTENSIONS_GO.read_text()
    block = re.search(r"var standardMembers = \[\]string\{(.*?)\}", src, re.S)
    assert block is not None
    go = re.findall(r'"([^"]+)"', block.group(1))
    assert go == _standard_members()


def test_build_problem_extensions() -> None:
    body = build_problem(
        TYPE_CONFLICT,
        detail="The tenant was modified concurrently.",
        instance="/api/tenants/t1",
        trace_id="abc",
        timestamp=_EPOCH,
        extensions={"record_version": 7, "active_workflows": ["wf-1"], "Zeta": True},
    )
    # Standard members first, in the specification's order, then the extensions sorted by name
    # (byte order: upper case first).
    assert _render(body) == (
        '{"type":"https://errors.company.com/platform/conflict","title":"Conflict","status":409,'
        '"detail":"The tenant was modified concurrently.","instance":"/api/tenants/t1",'
        '"traceId":"abc","errorCode":"CONFLICT","timestamp":"1970-01-01T00:00:00Z",'
        '"Zeta":true,"active_workflows":["wf-1"],"record_version":7}'
    )
    # Without extensions the body is exactly what it was before they existed.
    plain = build_problem(TYPE_CONFLICT, trace_id="abc", timestamp=_EPOCH)
    assert plain == build_problem(TYPE_CONFLICT, trace_id="abc", timestamp=_EPOCH, extensions={})
    assert list(plain) == ["type", "title", "status", "traceId", "errorCode", "timestamp"]


def test_build_problem_copies_extensions() -> None:
    ext: dict[str, Any] = {"licensed_seats": 10}
    body = build_problem(TYPE_CONFLICT, extensions=ext)
    ext["licensed_seats"] = 11
    ext["added_later"] = 1
    assert body["licensed_seats"] == 10 and "added_later" not in body


def test_collision_with_standard_members() -> None:
    """A standard member always wins: a colliding or invalid name is dropped, never sent."""
    body = build_problem(
        TYPE_NOT_FOUND,
        detail="Tender 1 was not found.",
        trace_id="trace",
        timestamp=_EPOCH,
        extensions={
            "type": "x",
            "title": "x",
            "status": 200,
            "detail": "x",
            "instance": "x",
            "traceId": "x",
            "errorCode": "x",
            "tenantId": "other-tenant",
            "timestamp": "x",
            "validationErrors": "x",
            "Status": 200,
            "TRACEID": "x",
            "ab": 1,
            "bad-name": 1,
            "kept_member": 1,
        },
    )
    assert _render(body) == (
        '{"type":"https://errors.company.com/platform/not-found","title":"Not Found",'
        '"status":404,"detail":"Tender 1 was not found.","traceId":"trace",'
        '"errorCode":"NOT_FOUND","timestamp":"1970-01-01T00:00:00Z","kept_member":1}'
    )
    only_invalid = build_problem(TYPE_NOT_FOUND, timestamp=_EPOCH, extensions={"status": 200})
    assert only_invalid["status"] == 404 and len(only_invalid) == 5


def test_internal_error_never_has_extensions() -> None:
    """An internal-error drops its extensions as it drops its detail."""
    body = build_problem(TYPE_INTERNAL_ERROR, detail="pq", extensions={"sql_state": "40P01"})
    assert "sql_state" not in body and "detail" not in body
    # A type without a status is sent as internal-error.
    assert "leaked" not in build_problem(ProblemType("", "", "", 0, ""), extensions={"leaked": 1})
    # Other 5xx types keep theirs, as they keep their (fixed) detail.
    body = build_problem(TYPE_SERVICE_UNAVAILABLE, extensions={"retry_after_seconds": 30})
    assert body["retry_after_seconds"] == 30


def test_round_trip() -> None:
    body = build_problem(
        TYPE_CONFLICT,
        detail="The tenant was modified concurrently.",
        trace_id="abc",
        timestamp=_EPOCH,
        extensions={
            "record_version": 9007199254740993,  # 2**53 + 1: not a float64
            "active_workflows": [{"id": "wf-1", "stage": "review"}],
            "nothing_here": None,
        },
    )
    wire = _render(body)
    p = parse_problem(json.loads(wire))
    assert (p.status, p.type, p.detail, p.trace_id, p.error_code) == (
        409,
        body["type"],
        "The tenant was modified concurrently.",
        "abc",
        "CONFLICT",
    )
    assert p.extensions == {
        "active_workflows": [{"id": "wf-1", "stage": "review"}],
        "nothing_here": None,
        "record_version": 9007199254740993,
    }
    assert p.extension("record_version", int) == 9007199254740993

    @dataclass
    class Workflow:
        id: str
        stage: str

    assert p.extension("active_workflows", list[Workflow]) == [Workflow("wf-1", "review")]
    # null leaves the default, as json.Unmarshal leaves its destination unchanged.
    assert "nothing_here" in p.extensions and p.extension("nothing_here", int) is None
    assert p.extension("nothing_here", int, 5) == 5


def test_problem_extension() -> None:
    p = Problem(
        type="about:blank",
        title="x",
        status=409,
        extensions={"record_version": "seven", "numeric_text": "7", "bad_value": math.inf},
    )
    assert p.extension("missing_member", int) is None
    assert p.extension("missing_member", int, -1) == -1
    with pytest.raises(ValueError, match="'record_version'"):
        p.extension("record_version", int)
    with pytest.raises(ValueError, match="'numeric_text'"):  # strict, as json.Unmarshal
        p.extension("numeric_text", int)
    with pytest.raises(ValueError, match="'bad_value'"):  # not JSON
        p.extension("bad_value", float)
    assert (
        Problem(type="about:blank", title="x", status=404).extension("record_version", int) is None
    )
    # extensions is not hashed, so a Problem stays hashable.
    assert hash(p) == hash(Problem(type="about:blank", title="x", status=409))


def test_from_response_extensions() -> None:
    """A third-party server's body: every member the parser does not recognise is kept,
    including names the builder would not send, and none overwrites a standard one."""
    body = (
        '{"type":"https://example.net/probs/out-of-credit","title":"You do not have enough '
        'credit.","status":403,"Status":200,"detail":"Your current balance is 30, but that '
        'costs 50.","instance":"/account/12345/msgs/abc","balance":30,'
        '"accounts":["/account/12345","/account/67890"],"x-vendor":{"a":1}}'
    )
    resp = SimpleNamespace(
        headers={"content-type": "application/problem+json"},
        status_code=403,
        json=lambda: json.loads(body),
    )
    p = problem_from_response(resp)
    assert p is not None
    assert (p.type, p.status, p.instance) == (
        "https://example.net/probs/out-of-credit",
        403,
        "/account/12345/msgs/abc",
    )
    assert p.extensions == {
        "balance": 30,
        "accounts": ["/account/12345", "/account/67890"],
        "x-vendor": {"a": 1},
    }
    assert p.extension("balance", int) == 30
    # No unrecognised member: extensions stays empty, so existing comparisons hold.
    plain = parse_problem({"type": "about:blank", "title": "Not Found", "status": 404})
    assert plain.extensions == {}
    assert plain == Problem(type="about:blank", title="Not Found", status=404)
