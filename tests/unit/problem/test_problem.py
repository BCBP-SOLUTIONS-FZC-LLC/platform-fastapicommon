from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastapicommon.problem import (
    PLATFORM_TYPES,
    TYPE_BAD_REQUEST,
    TYPE_INTERNAL_ERROR,
    TYPE_NOT_FOUND,
    TYPE_VALIDATION_FAILED,
    FieldError,
    ProblemType,
    build_problem,
    is_problem_content_type,
    parse_problem,
    problem_from_response,
    problem_type,
    resolve_trace_id,
    sanitized_request_id,
    trace_id_from_traceparent,
    type_for_status,
    type_uri,
    utc_timestamp,
)
from fastapicommon.problem.validation import field_message, field_path, validation_problem_parts

#: gincommon's registry of record, when the workspace's sibling checkout is present.
_GINCOMMON_REGISTRY_DOC = (
    Path(__file__).resolve().parents[5]
    / "platform-gincommon"
    / "docs"
    / "problem-details"
    / "registry.md"
)


@pytest.mark.skipif(
    not _GINCOMMON_REGISTRY_DOC.exists(), reason="platform-gincommon v2 not checked out"
)
def test_platform_types_match_gincommon_registry() -> None:
    doc = _GINCOMMON_REGISTRY_DOC.read_text()
    rows = re.findall(
        r"^\| `(platform/[a-z-]+)` \| (\d{3}) \| `([A-Z_]+)` \| ([^|]+) \|", doc, re.M
    )
    go = {(i, int(s), c, t.strip()) for i, s, c, t in rows}
    py = {(t.id, t.status, t.code, t.title) for t in PLATFORM_TYPES}
    assert py == go


def test_type_uri_and_status() -> None:
    assert TYPE_NOT_FOUND.uri() == "https://errors.company.com/platform/not-found"
    assert type_uri(" https://e.example.com// ", "tender", "x") == "https://e.example.com/tender/x"
    assert ProblemType("", "", "x", 418, "X").uri() == "about:blank"
    assert type_for_status(400) is TYPE_BAD_REQUEST
    for t in PLATFORM_TYPES:
        if t is not TYPE_VALIDATION_FAILED:
            assert type_for_status(t.status) == t
    teapot = type_for_status(418)
    assert (teapot.id, teapot.title, teapot.code) == ("", "I'm a Teapot", "I_M_A_TEAPOT")
    assert type_for_status(499).title == "Error"


@pytest.mark.parametrize(
    "args",
    [
        ("Tender", "x", "t", 400, "X"),
        ("platform", "x", "t", 400, "X"),
        ("tender", "not_found", "t", 400, "X"),
        ("tender", "x", " ", 400, "X"),
        ("tender", "x", "t", 302, "X"),
        ("tender", "x", "t", 400, "lower"),
    ],
)
def test_problem_type_rejects(args: tuple[str, str, str, int, str]) -> None:
    with pytest.raises(ValueError):
        problem_type(*args)


def test_build_problem() -> None:
    ts = datetime(2026, 10, 15, 10, 45, 22, tzinfo=UTC)
    body = build_problem(
        TYPE_NOT_FOUND,
        detail="Tender 123 was not found.",
        error_code="TENDER_NOT_FOUND",
        instance="/api/tenders/123",
        trace_id="abc",
        tenant_id="tenant-001",
        timestamp=ts,
    )
    assert body == {
        "type": "https://errors.company.com/platform/not-found",
        "title": "Not Found",
        "status": 404,
        "detail": "Tender 123 was not found.",
        "instance": "/api/tenders/123",
        "traceId": "abc",
        "errorCode": "TENDER_NOT_FOUND",
        "tenantId": "tenant-001",
        "timestamp": "2026-10-15T10:45:22Z",
    }
    internal = build_problem(
        TYPE_INTERNAL_ERROR, detail="pq: secret", validation_errors=[FieldError("a", "b")]
    )
    assert "detail" not in internal and "validationErrors" not in internal
    assert build_problem(ProblemType("", "", "", 0, ""))["status"] == 500
    assert build_problem(ProblemType("x", "y", "", 418, "X"))["title"] == "I'm a Teapot"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_timestamp())


# The same vectors as gincommon test/unit/problem and pgcommon-py.
_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"00-{_ID}-00f067aa0ba902b7-01", _ID),
        (f"  00-{_ID}-00f067aa0ba902b7-00 ", _ID),
        (f"01-{_ID}-00f067aa0ba902b7-01-future", _ID),
        (f"00-{_ID}-00f067aa0ba902b7-01-extra", None),
        (f"ff-{_ID}-00f067aa0ba902b7-01", None),
        (f"00-{_ID.upper()}-00f067aa0ba902b7-01", None),
        (f"00-{'0' * 32}-00f067aa0ba902b7-01", None),
        (f"00-{_ID}-{'0' * 16}-01", None),
        ("", None),
        (None, None),
    ],
)
def test_traceparent(value: str | None, expected: str | None) -> None:
    assert trace_id_from_traceparent(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("req-7f3a9c", "req-7f3a9c"),
        (" a:b.c_d ", "a:b.c_d"),
        ("x" * 128, "x" * 128),
        ("x" * 129, None),
        ("bad id", None),
        ("<script>", None),
        (None, None),
    ],
)
def test_request_id(value: str | None, expected: str | None) -> None:
    assert sanitized_request_id(value) == expected


def test_resolve_trace_id_order() -> None:
    tp = f"00-{_ID}-00f067aa0ba902b7-01"
    span = "0af7651916cd43dd8448eb211c80319c"
    assert resolve_trace_id(tp, "req-1", span) == _ID
    assert resolve_trace_id("bad", "req-1", span) == "req-1"
    assert resolve_trace_id(None, "bad id", span) == span
    assert re.fullmatch(r"[0-9a-f]{32}", resolve_trace_id(None, None, "0" * 32))


def test_validation_mapping() -> None:
    assert field_path(("body", "items", 0, "sku")) == "items[0].sku"
    assert field_path(("query", "page")) == "page"
    assert field_path(("items", 0, "sku")) == "items[0].sku"  # no location prefix to drop
    assert field_path(()) == ""
    cases = [
        ({"type": "string_too_short", "ctx": {"min_length": 3}}, "must be at least 3 characters"),
        ({"type": "too_short", "ctx": {"min_length": 1}}, "must be at least 1 items"),
        ({"type": "too_long", "ctx": {"max_length": 2}}, "must be at most 2 items"),
        ({"type": "less_than_equal", "ctx": {"le": 5}}, "must be at most 5"),
        ({"type": "greater_than", "ctx": {"gt": 1}}, "must be greater than 1"),
        ({"type": "less_than", "ctx": {"lt": 1}}, "must be less than 1"),
        ({"type": "enum", "ctx": {"expected": "'a', 'b' or 'c'"}}, "must be one of: a, b, c"),
        (
            {"type": "value_error", "msg": "value is not a valid email address"},
            "must be a valid email address",
        ),
        ({"type": "bool_parsing"}, "must be a boolean"),
        ({"type": "uuid_parsing"}, "must be a valid UUID"),
        ({"type": "url_parsing"}, "must be a valid URL"),
        ({"type": "datetime_from_date_parsing"}, "must be a valid date-time"),
        ({"type": "dict_type"}, "must be an object"),
        ({"type": "list_type"}, "must be an array"),
        ({"type": "custom"}, "is invalid"),
    ]
    for err, want in cases:
        assert field_message(err) == want, err
    assert validation_problem_parts([{"type": "json_invalid", "loc": ("body", 1)}]) == (
        "The request body is not valid JSON.",
        [],
    )


def test_client_parsing() -> None:
    body = {
        "type": "https://errors.company.com/tender/not-found",
        "title": "Tender not found",
        "detail": "x",
        "traceId": "t",
        "errorCode": "TENDER_NOT_FOUND",
        "future": {"a": 1},
        "validationErrors": [{"field": "a", "message": "b"}, "junk"],
    }
    p = parse_problem(body, 404)
    assert (p.status, p.type, p.error_code, p.trace_id) == (
        404,
        body["type"],
        "TENDER_NOT_FOUND",
        "t",
    )
    assert p.validation_errors == (FieldError("a", "b"),)
    resp = SimpleNamespace(
        headers={"content-type": "application/problem+json; charset=utf-8"},
        status_code=409,
        json=lambda: {"status": 409, "title": "Conflict"},
    )
    assert problem_from_response(resp).status == 409  # type: ignore[union-attr]
    resp.headers = {"content-type": "application/json"}
    assert problem_from_response(resp) is None
    assert not is_problem_content_type(None)
