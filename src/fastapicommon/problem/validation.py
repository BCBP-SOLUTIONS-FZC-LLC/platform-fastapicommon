"""Map pydantic / FastAPI validation errors to ``validationErrors`` with the same field paths
and messages platform-gincommon derives from go-playground/validator tags, so both languages
report an invalid field identically. A message never echoes the rejected value."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastapicommon.problem.core import FieldError

DETAIL_EMPTY_BODY = "The request body is empty."
DETAIL_INVALID_JSON = "The request body is not valid JSON."

_LOCATIONS = {"body", "query", "path", "header", "cookie"}

_TYPE_MESSAGES = {
    "string_type": "must be a string",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "int_from_float": "must be an integer",
    "float_type": "must be a number",
    "float_parsing": "must be a number",
    "decimal_type": "must be a number",
    "decimal_parsing": "must be a number",
    "bool_type": "must be a boolean",
    "bool_parsing": "must be a boolean",
    "list_type": "must be an array",
    "tuple_type": "must be an array",
    "set_type": "must be an array",
    "dict_type": "must be an object",
    "model_type": "must be an object",
    "model_attributes_type": "must be an object",
    "uuid_type": "must be a valid UUID",
    "uuid_parsing": "must be a valid UUID",
    "url_type": "must be a valid URL",
    "url_parsing": "must be a valid URL",
    "url_scheme": "must be a valid URL",
    "datetime_type": "must be a valid date-time",
    "datetime_parsing": "must be a valid date-time",
    "datetime_from_date_parsing": "must be a valid date-time",
    "date_type": "must be a valid date-time",
    "date_parsing": "must be a valid date-time",
}


def field_path(loc: Sequence[Any]) -> str:
    """``("body", "items", 0, "sku")`` → ``items[0].sku`` (the location prefix is dropped)."""
    parts = list(loc)
    if parts and parts[0] in _LOCATIONS:
        parts = parts[1:]
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out


def _quoted(expected: str) -> list[str]:
    values = re.findall(r"'([^']*)'", expected)
    return values or [expected]


def field_message(err: Mapping[str, Any]) -> str:
    t = str(err.get("type", ""))
    ctx: Mapping[str, Any] = err.get("ctx") or {}
    if t == "missing":
        return "is required"
    if t in ("string_too_short", "too_short"):
        unit = " characters" if t == "string_too_short" else " items"
        return f"must be at least {ctx.get('min_length')}{unit}"
    if t in ("string_too_long", "too_long"):
        unit = " characters" if t == "string_too_long" else " items"
        return f"must be at most {ctx.get('max_length')}{unit}"
    if t == "greater_than_equal":
        return f"must be at least {ctx.get('ge')}"
    if t == "less_than_equal":
        return f"must be at most {ctx.get('le')}"
    if t == "greater_than":
        return f"must be greater than {ctx.get('gt')}"
    if t == "less_than":
        return f"must be less than {ctx.get('lt')}"
    if t in ("literal_error", "enum"):
        return "must be one of: " + ", ".join(_quoted(str(ctx.get("expected", ""))))
    if t == "value_error" and "email" in str(err.get("msg", "")).lower():
        return "must be a valid email address"
    return _TYPE_MESSAGES.get(t, "is invalid")


def validation_problem_parts(
    errors: Iterable[Mapping[str, Any]],
) -> tuple[str | None, list[FieldError]]:
    """``(detail, fields)`` for a RequestValidationError's ``errors()``: a missing or
    malformed body is a fixed detail with no fields; anything else is one entry per error."""
    errs = list(errors)
    for e in errs:
        if e.get("type") == "json_invalid":
            return DETAIL_INVALID_JSON, []
    if (
        len(errs) == 1
        and errs[0].get("type") == "missing"
        and tuple(errs[0].get("loc", ())) == ("body",)
    ):
        return DETAIL_EMPTY_BODY, []
    return None, [FieldError(field_path(e.get("loc", ())), field_message(e)) for e in errs]
