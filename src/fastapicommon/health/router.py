from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], "Awaitable[bool] | bool"]


def create_health_router(readiness_checks: list[ReadinessCheck] | None = None) -> APIRouter:
    """Build a router exposing ``/health``, ``/live``, and ``/ready``.

    ``/health`` and ``/live`` report the process is up. ``/ready`` additionally runs
    ``readiness_checks`` (e.g. database connectivity) and returns 503 if any fail.
    """
    router = APIRouter()
    checks = readiness_checks or []

    @router.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        for check in checks:
            result = check()
            if isinstance(result, Awaitable):
                result = await result
            if not result:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"status": "not_ready"},
                )
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})

    return router


health_router = create_health_router()
