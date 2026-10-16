"""Structured logging with platform-gincommon's output contract (its Zap logger,
``internal/adapter/outbound/logger/zap.go``), so Go and Python services read the same in Loki:

- **production** (every environment except ``dev`` / ``local``): one JSON object per line on
  stderr, Zap's production layout — ``level`` (lowercase), ``ts`` (Unix seconds, float),
  ``caller`` (``dir/file.py:line``), ``msg``, then the entry's fields, and ``stacktrace`` (the
  call stack) on error entries;
- **development** (``dev`` / ``local``): Zap's console layout on stderr —
  ``<ISO8601 time>\\t<LEVEL>\\t<caller>\\t<msg>\\t<fields as JSON>``, with the stack trace from
  warn up;
- ``LOG_LEVEL`` (``debug``, ``info``, ``warn``, ``error``) overrides the default level (debug in
  dev/local, info otherwise); an invalid value keeps the default and warns once on stderr;
- ``LOG_SAMPLING=true`` enables Zap's sampling: per second, per message and level, the first 100
  entries are kept, then every 100th. Off by default, so every ``http_request`` is logged;
- ``LOG_FILE`` (e.g. ``logs/app.log`` for Promtail) also writes JSON there (``ts`` as ISO8601),
  with the same level and sampling.

Entries logged inside a request also carry the request context (``request_id``,
``correlation_id``, ``trace_id``, and ``tenant_id`` / ``user_id`` when set), unless the entry
sets them itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from fastapicommon.context import get_request_context_or_none

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
#: Zap's level names (``warning`` is ``warn``; ``critical`` / ``exception`` map to ``error``).
_ZAP_LEVEL = {
    "debug": "debug",
    "info": "info",
    "warning": "warn",
    "warn": "warn",
    "error": "error",
    "exception": "error",
    "critical": "error",
}

#: The innermost frames of the ``stacktrace`` field (Zap writes the goroutine's stack; a Python
#: call stack through Starlette and anyio is several times deeper, so it is capped).
MAX_STACK_FRAMES = 16

SAMPLING_TICK_SECONDS = 1.0
SAMPLING_INITIAL = 100
SAMPLING_THEREAFTER = 100

_warned: set[str] = set()
_HANDLER_MARKER = "_fastapicommon_handler"


def is_dev_env(environment: str | None) -> bool:
    """``dev`` and ``local`` (trimmed, case-insensitive) are developer environments; everything
    else gets production defaults (Go ``config.IsDevEnv``)."""
    return (environment or "").strip().lower() in ("dev", "local")


def _warn_once(key: str, value: str, using: str) -> None:
    marker = f"{key}={value}"
    if marker in _warned:
        return
    _warned.add(marker)
    print(f"fastapicommon: invalid {key}={value!r}; using {using}", file=sys.stderr)


def resolve_level(level: int | str | None, dev: bool) -> int:
    """An explicit ``level``, else ``LOG_LEVEL``, else debug (dev) / info (Go ``resolveLevel``)."""
    default = logging.DEBUG if dev else logging.INFO
    if isinstance(level, int):
        return level
    raw = (level if level else os.environ.get("LOG_LEVEL", "")).strip().lower()
    if not raw:
        return default
    if raw in _LEVELS:
        return _LEVELS[raw]
    _warn_once(
        "LOG_LEVEL",
        raw,
        f"{logging.getLevelName(default).lower()} (want debug, info, warn or error)",
    )
    return default


def sampling_enabled() -> bool:
    raw = os.environ.get("LOG_SAMPLING", "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw not in ("", "false", "0", "no"):
        _warn_once("LOG_SAMPLING", raw, "false")
    return False


# --------------------------------------------------------------------------------------------
# Processors
# --------------------------------------------------------------------------------------------


def _add_request_context(
    logger: WrappedLogger, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    context = get_request_context_or_none()
    if context is not None:
        event_dict.setdefault("request_id", context.request_id)
        event_dict.setdefault("correlation_id", context.correlation_id)
        if context.trace_id:
            event_dict.setdefault("trace_id", context.trace_id)
        if context.tenant_id:
            event_dict.setdefault("tenant_id", context.tenant_id)
        if context.user_id:
            event_dict.setdefault("user_id", context.user_id)
    return event_dict


class _Sampler:
    """Zap's ``NewSamplerWithOptions(core, 1s, 100, 100)``: per tick, per message and level, keep
    the first ``initial`` entries, then every ``thereafter``-th."""

    def __init__(
        self,
        tick: float = SAMPLING_TICK_SECONDS,
        initial: int = SAMPLING_INITIAL,
        thereafter: int = SAMPLING_THEREAFTER,
    ) -> None:
        self._tick, self._initial, self._thereafter = tick, initial, thereafter
        self._counts: dict[tuple[str, str], tuple[float, int]] = {}
        self._lock = threading.Lock()

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        key = (str(event_dict.get("event", "")), method_name)
        now = time.monotonic()
        with self._lock:
            window, count = self._counts.get(key, (now, 0))
            if now - window >= self._tick:
                window, count = now, 0
            count += 1
            self._counts[key] = (window, count)
        if count > self._initial and (count - self._initial) % self._thereafter != 0:
            raise structlog.DropEvent
        return event_dict


def _stack_from(dev: bool) -> int:
    return logging.WARNING if dev else logging.ERROR


def _make_caller_and_stack(dev: bool) -> structlog.typing.Processor:
    """Zap's ``AddCaller`` (``dir/file.py:line`` of the logging call) and ``AddStacktrace``
    (the call stack, from warn in dev / from error in production)."""
    threshold = _stack_from(dev)

    def processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        frame, _ = structlog._frames._find_first_app_frame_and_name(  # noqa: SLF001
            additional_ignores=["logging", "fastapicommon.logging.config"]
        )
        path = Path(frame.f_code.co_filename)
        event_dict["_caller"] = f"{path.parent.name}/{path.name}:{frame.f_lineno}"
        level = _LEVELS.get(_ZAP_LEVEL.get(method_name, method_name), logging.INFO)
        if level >= threshold:
            frames = traceback.format_stack(frame)[-MAX_STACK_FRAMES:]
            event_dict["_stacktrace"] = "".join(frames).rstrip("\n")
        return event_dict

    return processor


def _zap_entry(event_dict: EventDict, method_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split an event into Zap's header (level, caller, msg) and its fields."""
    fields = dict(event_dict)
    msg = fields.pop("event", "")
    level = _ZAP_LEVEL.get(fields.pop("level", method_name), "info")
    caller = fields.pop("_caller", None)
    stack = fields.pop("_stacktrace", None)
    fields.pop("timestamp", None)
    header = {"level": level, "caller": caller, "msg": msg, "stacktrace": stack}
    return header, fields


def _json_default(value: Any) -> Any:
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)


class ZapJSONRenderer:
    """Zap's JSON encoder layout: ``level``, ``ts``, ``caller``, ``msg``, fields, ``stacktrace``.
    ``iso_time`` renders ``ts`` as ISO8601 (the ``LOG_FILE`` encoder) instead of Unix seconds."""

    def __init__(self, iso_time: bool = False) -> None:
        self._iso_time = iso_time

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> str:
        header, fields = _zap_entry(event_dict, method_name)
        now = time.time()
        out: dict[str, Any] = {
            "level": header["level"],
            "ts": _iso8601(now) if self._iso_time else now,
        }
        if header["caller"]:
            out["caller"] = header["caller"]
        out["msg"] = header["msg"]
        out.update(fields)
        if header["stacktrace"]:
            out["stacktrace"] = header["stacktrace"]
        return json.dumps(out, default=_json_default, ensure_ascii=False)


class ZapConsoleRenderer:
    """Zap's development console layout: tab-separated time, LEVEL, caller, msg and the fields as
    JSON, with the stack trace on the following lines."""

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> str:
        header, fields = _zap_entry(event_dict, method_name)
        parts = [_iso8601(time.time()), header["level"].upper()]
        if header["caller"]:
            parts.append(header["caller"])
        parts.append(str(header["msg"]))
        if fields:
            parts.append(json.dumps(fields, default=_json_default, ensure_ascii=False))
        line = "\t".join(parts)
        if header["stacktrace"]:
            line += "\n" + header["stacktrace"]
        return line


def _iso8601(seconds: float) -> str:
    """Zap's ``ISO8601TimeEncoder``: ``2006-01-02T15:04:05.000Z0700`` (UTC here)."""
    moment = datetime.fromtimestamp(seconds, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


def configure_logging(level: int | str | None = None, environment: str | None = None) -> None:
    """Configure structlog (and the stdlib root logger it writes through) with gincommon's log
    contract; see the module docstring. ``environment`` defaults to ``APP_ENV``, then
    ``ENVIRONMENT``. Safe to call again (e.g. one call per app): the handlers are replaced.
    ``create_platform_app`` calls it with the settings' ``log_level`` and ``environment``.
    """
    env = (
        environment
        if environment is not None
        else (os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT", ""))
    )
    dev = is_dev_env(env)
    resolved = resolve_level(level, dev)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _add_request_context,
    ]
    if sampling_enabled():
        shared.append(_Sampler())
    shared += [
        _make_caller_and_stack(dev),
        structlog.processors.format_exc_info,
    ]

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]:
        root.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=ZapConsoleRenderer() if dev else ZapJSONRenderer(),
            foreign_pre_chain=shared,
        )
    )
    handlers: list[logging.Handler] = [console]
    log_file = os.environ.get("LOG_FILE", "")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=ZapJSONRenderer(iso_time=True), foreign_pre_chain=shared
            )
        )
        handlers.append(file_handler)
    for handler in handlers:
        setattr(handler, _HANDLER_MARKER, True)
        handler.setLevel(resolved)
        root.addHandler(handler)
    root.setLevel(resolved)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(resolved),
        cache_logger_on_first_use=False,
    )
