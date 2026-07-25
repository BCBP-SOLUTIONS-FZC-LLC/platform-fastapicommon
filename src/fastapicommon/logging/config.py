from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from fastapicommon.context import get_request_context_or_none


def _add_request_context(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    context = get_request_context_or_none()
    if context is not None:
        event_dict.setdefault("request_id", context.request_id)
        event_dict.setdefault("correlation_id", context.correlation_id)
        if context.tenant_id:
            event_dict.setdefault("tenant_id", context.tenant_id)
        if context.user_id:
            event_dict.setdefault("user_id", context.user_id)
    return event_dict


def configure_logging(level: int | str = logging.INFO) -> None:
    """Configure structlog for JSON-only output enriched with request context.

    Per ADR-001, this library standardizes on structlog with JSON output only (no
    console/pretty renderer) so log shipping is consistent across environments. Called
    once at service startup; fastapicommon.bootstrap.create_platform_app does this
    automatically.
    """
    resolved_level = logging.getLevelName(level) if isinstance(level, str) else level

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=resolved_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_request_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        cache_logger_on_first_use=True,
    )
