"""gincommon's log output contract (Zap layout, LOG_LEVEL / LOG_SAMPLING / LOG_FILE, sanitising)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from fastapicommon.context import CurrentUser, RequestContext, request_context_scope
from fastapicommon.logging import config as log_config
from fastapicommon.logging import configure_logging, get_logger
from fastapicommon.logging.sanitize import (
    is_sensitive_log_key,
    sanitize_fields,
    sanitize_log_value,
)


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ("LOG_LEVEL", "LOG_SAMPLING", "LOG_FILE", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    log_config._warned.clear()
    yield
    configure_logging("INFO", "prod")


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]


def test_production_json_layout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(environment="prod")
    get_logger("svc").info("orders_synced", count=3, tags={"a"})
    get_logger("svc").debug("not at info")
    (line,) = _lines(capsys)
    entry = json.loads(line)
    assert list(entry)[:4] == ["level", "ts", "caller", "msg"]
    assert entry["level"] == "info" and entry["msg"] == "orders_synced"
    assert isinstance(entry["ts"], float)
    assert entry["caller"].startswith("logging/test_log_contract.py:")
    assert entry["count"] == 3 and entry["tags"] == ["a"]
    assert "stacktrace" not in entry


def test_error_entries_carry_a_stacktrace(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(environment="staging")
    get_logger("svc").warning("w")
    get_logger("svc").error("e")
    warn, error = (json.loads(line) for line in _lines(capsys))
    assert warn["level"] == "warn" and "stacktrace" not in warn
    assert error["level"] == "error" and "test_log_contract.py" in error["stacktrace"]
    assert list(error)[-1] == "stacktrace"
    assert error["stacktrace"].count('File "') <= log_config.MAX_STACK_FRAMES


def test_development_console_layout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(environment="Dev")
    get_logger("svc").debug("hello", user="u1")
    get_logger("svc").warning("careful")
    get_logger("svc").info("bare")
    first, *rest = capsys.readouterr().err.splitlines()
    timestamp, level, caller, msg, fields = first.split("\t")
    assert timestamp.endswith("Z") and "T" in timestamp
    assert (level, msg, json.loads(fields)) == ("DEBUG", "hello", {"user": "u1"})
    assert caller.startswith("logging/test_log_contract.py:")
    warn_index = next(i for i, line in enumerate(rest) if "\tWARN\t" in line)
    assert rest[warn_index + 1].lstrip().startswith("File ")  # stack from warn in dev
    assert rest[-1].split("\t")[3] == "bare" and len(rest[-1].split("\t")) == 4


def test_environment_defaults_to_app_env(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    configure_logging()
    get_logger("svc").debug("dev default level")
    assert "\tDEBUG\t" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "level"),
    [("debug", logging.DEBUG), ("WARN", logging.WARNING), ("warning", logging.WARNING)],
)
def test_log_level_env(raw: str, level: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", raw)
    assert log_config.resolve_level(None, dev=False) == level
    assert log_config.resolve_level("error", dev=False) == logging.ERROR
    assert log_config.resolve_level(logging.INFO, dev=True) == logging.INFO


def test_invalid_log_level_warns_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "loud")
    assert log_config.resolve_level(None, dev=True) == logging.DEBUG
    assert log_config.resolve_level(None, dev=False) == logging.INFO
    assert capsys.readouterr().err.count("invalid LOG_LEVEL='loud'") == 1


def test_sampling(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_SAMPLING", "true")
    assert log_config.sampling_enabled()
    configure_logging(environment="prod")
    logger = get_logger("svc")
    for _ in range(250):
        logger.info("hot")
    logger.warning("hot")  # sampled per message AND level
    lines = _lines(capsys)
    assert sum('"msg": "hot"' in line and '"info"' in line for line in lines) == 101
    assert sum('"warn"' in line for line in lines) == 1

    monkeypatch.setenv("LOG_SAMPLING", "maybe")
    assert not log_config.sampling_enabled()
    assert "invalid LOG_SAMPLING" in capsys.readouterr().err


def _record(msg: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("t", level, __file__, 1, msg, None, None)


def test_sampler_window_resets() -> None:
    sampler = log_config._SamplingFilter(tick=0.0, initial=1, thereafter=100)
    for _ in range(3):
        assert sampler.filter(_record({"event": "x"}))
    strict = log_config._SamplingFilter(tick=60, initial=1, thereafter=2)
    assert strict.filter(_record({"event": "y"}))
    assert not strict.filter(_record({"event": "y"}))
    assert strict.filter(_record({"event": "y"}))


def test_sampler_decides_once_per_record() -> None:
    """stderr and LOG_FILE share the filter: one record is counted once, same decision in both."""
    strict = log_config._SamplingFilter(tick=60, initial=1, thereafter=1000)
    first, second = _record("z"), _record("z")
    assert strict.filter(first) and strict.filter(first)
    assert not strict.filter(second) and not strict.filter(second)


def test_sampler_table_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: one counter per distinct message, never evicted (f-string messages from
    libraries grow it for the life of the process)."""
    monkeypatch.setattr(log_config, "MAX_SAMPLING_KEYS", 50)
    sampler = log_config._SamplingFilter(tick=60)
    for i in range(500):
        assert sampler.filter(_record(f"retrying attempt {i}"))
    assert len(sampler._counts) <= 50
    expired = log_config._SamplingFilter(tick=0.0)
    for i in range(20):
        expired.filter(_record(f"m{i}"))
    assert len(expired._counts) <= 1


def test_sampling_never_raises_into_stdlib_callers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the sampler used to raise structlog.DropEvent (a BaseException) out of the
    ProcessorFormatter into whoever called a stdlib logger, from the 101st identical entry."""
    monkeypatch.setenv("LOG_SAMPLING", "true")
    configure_logging(environment="prod")
    lib = logging.getLogger("some.library")
    for i in range(250):
        lib.info("hot %s", i)  # must not raise
    lines = _lines(capsys)
    assert sum('"msg": "hot ' in line for line in lines) == 101


def test_log_file_tees_json_with_iso_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "logs" / "app.log"
    monkeypatch.setenv("LOG_FILE", str(target))
    configure_logging(environment="dev")
    get_logger("svc").info("to both")
    logging.getLogger("uvicorn.error").warning("foreign %s", "entry")
    assert "\tINFO\t" in capsys.readouterr().err
    entries = [json.loads(line) for line in target.read_text().splitlines()]
    assert entries[0]["msg"] == "to both" and entries[0]["ts"].endswith("Z")
    assert entries[1]["msg"] == "foreign entry" and entries[1]["level"] == "warn"


def test_request_context_enrichment(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(environment="prod")
    context = RequestContext(
        request_id="r1",
        correlation_id="c1",
        user=CurrentUser(user_id="u1", tenant_id="t1"),
        trace_id="a" * 32,
    )
    with request_context_scope(context):
        get_logger("svc").info("inside", user_id="explicit")
    entry = json.loads(_lines(capsys)[0])
    assert entry["request_id"] == "r1" and entry["trace_id"] == "a" * 32
    assert entry["tenant_id"] == "t1" and entry["user_id"] == "explicit"


def test_exceptions_are_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(environment="prod")
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("svc").exception("failed")
    entry = json.loads(_lines(capsys)[0])
    assert entry["level"] == "error" and "ValueError" in entry["exception"]


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("password", True),
        ("db_password", True),
        ("Authorization", True),
        ("refresh_token", True),
        ("session", True),
        ("signing_key", True),
        ("key", True),
        ("monkey", False),
        ("key_metrics", False),
        ("route", False),
        ("", False),
    ],
)
def test_sensitive_keys(key: str, sensitive: bool) -> None:
    assert is_sensitive_log_key(key) is sensitive


def test_sanitize_fields_and_values() -> None:
    assert sanitize_fields({"token": "t", "route": "/x"}) == {"token": "[REDACTED]", "route": "/x"}
    assert sanitize_log_value("a\nb\x00c\td") == "abcd"
    cut = sanitize_log_value("é" * 200)  # 400 bytes
    assert len(cut.encode()) <= 256 and cut == "é" * 128
    assert len(sanitize_log_value("x" * 300)) == 256


def test_is_dev_env() -> None:
    assert log_config.is_dev_env(" LOCAL ") and log_config.is_dev_env("dev")
    assert not log_config.is_dev_env("development") and not log_config.is_dev_env(None)
