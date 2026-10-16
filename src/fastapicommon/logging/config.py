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
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
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
    # Accepted for services configured before the gincommon contract (stdlib names) and for
    # Zap's fatal: only critical / fatal entries are written; they render as "error".
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
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
#: Upper bound of the sampler's per-message counter table.
MAX_SAMPLING_KEYS = 4096

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
        f"{logging.getLevelName(default).lower()} (want debug, info, warn, error or critical)",
    )
    return default


def env_bool(key: str, fallback: bool) -> bool:
    """A boolean environment variable (``true`` / ``1`` / ``yes``, ``false`` / ``0`` / ``no``,
    case-insensitive); unset gives ``fallback``, and an invalid value gives ``fallback`` with a
    one-time warning on stderr. Shared by the logging and tracing configuration."""
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    if raw:
        _warn_once(key, raw, str(fallback).lower())
    return fallback


def sampling_enabled() -> bool:
    return env_bool("LOG_SAMPLING", False)


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


class _SamplingFilter(logging.Filter):
    """Zap's ``NewSamplerWithOptions(core, 1s, 100, 100)``: per tick, per message and level, keep
    the first ``initial`` entries, then every ``thereafter``-th.

    A handler filter rather than a structlog processor, so a dropped entry is simply not
    written (raising ``structlog.DropEvent`` from a ``ProcessorFormatter`` would propagate
    into the caller of a stdlib logger). One instance is shared by every handler and decides
    once per record, so an entry written to stderr and ``LOG_FILE`` is counted once and kept
    or dropped in both."""

    _DECISION = "_fastapicommon_sampled"

    def __init__(
        self,
        tick: float = SAMPLING_TICK_SECONDS,
        initial: int = SAMPLING_INITIAL,
        thereafter: int = SAMPLING_THEREAFTER,
    ) -> None:
        super().__init__()
        self._tick, self._initial, self._thereafter = tick, initial, thereafter
        self._counts: dict[tuple[str, int], tuple[float, int]] = {}
        self._swept = time.monotonic()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        decision = getattr(record, self._DECISION, None)
        if decision is None:
            decision = self._keep(_record_message(record), record.levelno)
            setattr(record, self._DECISION, decision)
        return bool(decision)

    def _keep(self, message: str, level: int) -> bool:
        key = (message, level)
        now = time.monotonic()
        with self._lock:
            if now - self._swept >= self._tick or len(self._counts) >= MAX_SAMPLING_KEYS:
                self._sweep(now)
            window, count = self._counts.get(key, (now, 0))
            if now - window >= self._tick:
                window, count = now, 0
            count += 1
            self._counts[key] = (window, count)
        return count <= self._initial or (count - self._initial) % self._thereafter == 0

    def _sweep(self, now: float) -> None:
        """Drop counters whose window has ended (they would restart at 0 anyway), so distinct
        messages (formatted before logging) cannot grow the table without bound; past
        ``MAX_SAMPLING_KEYS`` live keys, start over (Zap's table is fixed-size too)."""
        self._counts = {k: v for k, v in self._counts.items() if now - v[0] < self._tick}
        if len(self._counts) >= MAX_SAMPLING_KEYS:
            self._counts.clear()
        self._swept = now


def _record_message(record: logging.LogRecord) -> str:
    """The entry's ``msg``: the structlog event, else the stdlib message template."""
    if isinstance(record.msg, dict):
        return str(record.msg.get("event", ""))
    return str(record.msg)


#: Modules whose frames are logging machinery, not the caller (exact name or a submodule).
_IGNORED_MODULES = ("structlog", "logging", __name__)


def _is_ignored_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in _IGNORED_MODULES)


def _first_app_frame() -> FrameType:
    """The innermost frame outside structlog, the stdlib ``logging`` package and this module:
    the code that made the logging call."""
    frame = sys._getframe(1)
    while frame.f_back is not None and _is_ignored_module(frame.f_globals.get("__name__") or ""):
        frame = frame.f_back
    return frame


def _stack_from(dev: bool) -> int:
    return logging.WARNING if dev else logging.ERROR


#: The level from which entries carry ``stacktrace`` (set by :func:`configure_logging`; read
#: per entry so cached loggers follow a reconfiguration).
_stack_threshold = logging.ERROR


def _zap_stack(frame: FrameType) -> str:
    """Zap's stacktrace layout, innermost first: ``function`` then ``\tfile:line`` per frame, at
    most ``MAX_STACK_FRAMES``. Built from the frames alone (no source lines are read)."""
    lines: list[str] = []
    current: FrameType | None = frame
    while current is not None and len(lines) < 2 * MAX_STACK_FRAMES:
        code = current.f_code
        module = current.f_globals.get("__name__") or "?"
        lines.append(f"{module}.{getattr(code, 'co_qualname', code.co_name)}")
        lines.append(f"\t{code.co_filename}:{current.f_lineno}")
        current = current.f_back
    return "\n".join(lines)


def _caller_and_stack(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Zap's ``AddCaller`` (``dir/file.py:line`` of the logging call) and ``AddStacktrace``
    (the call stack, from warn in dev / from error in production)."""
    frame = _first_app_frame()
    path = Path(frame.f_code.co_filename)
    event_dict["_caller"] = f"{path.parent.name}/{path.name}:{frame.f_lineno}"
    level = _LEVELS.get(_ZAP_LEVEL.get(method_name, method_name), logging.INFO)
    if level >= _stack_threshold:
        event_dict["_stacktrace"] = _zap_stack(frame)
    return event_dict


#: One processor chain for every configuration: what changes between calls (stack threshold,
#: level, handlers) is read at log time, so loggers can be cached on first use.
_SHARED: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    _add_request_context,
    _caller_and_stack,
    structlog.processors.format_exc_info,
]


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
    global _stack_threshold
    dev = is_dev_env(env)
    resolved = resolve_level(level, dev)
    _stack_threshold = _stack_from(dev)
    shared = _SHARED

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
    sampler = _SamplingFilter() if sampling_enabled() else None
    for handler in handlers:
        setattr(handler, _HANDLER_MARKER, True)
        if sampler is not None:
            handler.addFilter(sampler)
        handler.setLevel(resolved)
        root.addHandler(handler)
    root.setLevel(resolved)

    structlog.configure(
        # filter_by_level checks the stdlib level at call time (set above), so a cached logger
        # follows a later configure_logging, and a disabled entry costs one level check.
        processors=_PROCESSORS,
        logger_factory=_LOGGER_FACTORY,
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


_PROCESSORS: list[structlog.typing.Processor] = [
    structlog.stdlib.filter_by_level,
    *_SHARED,
    structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
]
_LOGGER_FACTORY = structlog.stdlib.LoggerFactory()
