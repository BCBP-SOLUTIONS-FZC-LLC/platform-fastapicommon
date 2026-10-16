from __future__ import annotations

from fastapi import APIRouter
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from fastapicommon.metrics.platform import metrics_registry

metrics_router = APIRouter()


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(metrics_registry()), media_type=CONTENT_TYPE_LATEST)
