from __future__ import annotations

from typing import cast

import structlog


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger enriched with request/tenant/user context.

    Usage: ``logger = get_logger(__name__)``.
    """
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
