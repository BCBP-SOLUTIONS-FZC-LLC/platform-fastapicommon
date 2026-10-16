from __future__ import annotations

import threading
import time

from fastapi import APIRouter
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from fastapicommon.logging import get_logger
from fastapicommon.logging.sanitize import sanitize_log_value
from fastapicommon.metrics.platform import metrics_registry

metrics_router = APIRouter()

_logger = get_logger("fastapicommon.metrics")

#: Go ``MetricsHandler``: each distinct error is logged when first seen, then at most once per
#: interval, so a persistently broken collector does not log on every scrape.
METRICS_ERROR_LOG_INTERVAL_SECONDS = 300.0
_MAX_TRACKED_ERRORS = 64
_last_logged: dict[str, float] = {}
_lock = threading.Lock()


def _log_metrics_error(error: str) -> None:
    now = time.monotonic()
    with _lock:
        last = _last_logged.get(error)
        if last is not None and now - last < METRICS_ERROR_LOG_INTERVAL_SECONDS:
            return
        if len(_last_logged) >= _MAX_TRACKED_ERRORS and error not in _last_logged:
            _last_logged.pop(next(iter(_last_logged)))
        _last_logged[error] = now
    _logger.error("metrics handler error", error=error)


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    try:
        body = generate_latest(metrics_registry())
    except Exception as exc:
        # prometheus_client cannot serve a partial page (Go: promhttp.ContinueOnError), so the
        # scrape fails; the error is logged, rate-limited, as Go does.
        _log_metrics_error(sanitize_log_value(f"{type(exc).__name__}: {exc}"))
        return Response(status_code=500, content=b"metrics collection failed\n")
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
