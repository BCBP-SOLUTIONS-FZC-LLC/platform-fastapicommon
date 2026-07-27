import pytest

from fastapicommon.auth.validation import normalize_header_value, normalize_request_id, parse_roles

# --- normalize_header_value ---


def test_normalize_header_value_accepts_plain_value() -> None:
    assert normalize_header_value("acme-corp") == "acme-corp"


def test_normalize_header_value_trims_whitespace() -> None:
    assert normalize_header_value("  acme-corp  ") == "acme-corp"


def test_normalize_header_value_returns_none_for_missing() -> None:
    assert normalize_header_value(None) is None


def test_normalize_header_value_returns_none_for_empty_or_whitespace_only() -> None:
    assert normalize_header_value("") is None
    assert normalize_header_value("   ") is None


def test_normalize_header_value_returns_none_when_too_long() -> None:
    assert normalize_header_value("a" * 257) is None
    assert normalize_header_value("a" * 256) == "a" * 256


@pytest.mark.parametrize("bad_char", ["\x00", "\x1f", "\x7f", " ", " ", "value\nwith\nnewline"])
def test_normalize_header_value_rejects_control_characters(bad_char: str) -> None:
    assert normalize_header_value(f"acme{bad_char}corp") is None


# --- normalize_request_id ---


def test_normalize_request_id_accepts_valid_token() -> None:
    assert normalize_request_id("req-123.abc_DEF") == "req-123.abc_DEF"


def test_normalize_request_id_returns_none_for_missing_or_empty() -> None:
    assert normalize_request_id(None) is None
    assert normalize_request_id("") is None
    assert normalize_request_id("   ") is None


def test_normalize_request_id_returns_none_when_too_long() -> None:
    assert normalize_request_id("a" * 129) is None
    assert normalize_request_id("a" * 128) == "a" * 128


@pytest.mark.parametrize("bad_id", ["has space", "has/slash", "has,comma", "emoji😀"])
def test_normalize_request_id_returns_none_for_invalid_characters(bad_id: str) -> None:
    assert normalize_request_id(bad_id) is None


# --- parse_roles ---


def test_parse_roles_splits_trims_and_validates() -> None:
    assert parse_roles("admin, operator , viewer") == ("admin", "operator", "viewer")


def test_parse_roles_returns_empty_tuple_for_missing_or_empty_header() -> None:
    assert parse_roles(None) == ()
    assert parse_roles("") == ()


def test_parse_roles_deduplicates_preserving_first_occurrence_order() -> None:
    assert parse_roles("admin,operator,admin") == ("admin", "operator")


def test_parse_roles_silently_drops_invalid_tokens_without_failing() -> None:
    assert parse_roles("admin,has space,has/slash,operator") == ("admin", "operator")


def test_parse_roles_drops_empty_tokens_from_stray_commas() -> None:
    assert parse_roles("admin,,operator,") == ("admin", "operator")


def test_parse_roles_drops_tokens_exceeding_max_length() -> None:
    long_role = "a" * 65
    assert parse_roles(f"admin,{long_role}") == ("admin",)
    assert parse_roles(f"admin,{'a' * 64}") == ("admin", "a" * 64)
