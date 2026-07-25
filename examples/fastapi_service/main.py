"""Example consumer service built on fastapicommon.

Run with: make run (see Makefile), or directly:
  uvicorn examples.fastapi_service.main:app --host 0.0.0.0 --port 8080 --reload

Reads fastapicommon.config.PlatformSettings from the environment / .env — see
.env-example for the full list (APP_NAME, APP_PORT, OTLP_ENDPOINT, etc.).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from fastapicommon.auth import CurrentUser, get_current_user
from fastapicommon.bootstrap import create_platform_app
from fastapicommon.config import PlatformSettings
from fastapicommon.exceptions import NotFoundException
from fastapicommon.logging import get_logger
from fastapicommon.rbac import RequireRole

logger = get_logger(__name__)

app = create_platform_app(settings=PlatformSettings())

router = APIRouter(prefix="/widgets", tags=["widgets"])


@router.get("/me")
async def whoami(user: CurrentUser = Depends(get_current_user)) -> dict[str, object]:
    """Service-owned route: everything below this line is business logic, not part of
    fastapicommon."""
    logger.info("whoami_requested", user_id=user.user_id)
    return {"user_id": user.user_id, "tenant_id": user.tenant_id, "roles": user.roles}


@router.delete("/{widget_id}", dependencies=[Depends(RequireRole("tenant_admin"))])
async def delete_widget(widget_id: str) -> dict[str, str]:
    if widget_id == "missing":
        raise NotFoundException(f"widget {widget_id} not found")
    return {"status": "deleted"}


app.include_router(router)
