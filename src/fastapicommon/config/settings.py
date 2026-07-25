from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformSettings(BaseSettings):
    """Base settings shared by all platform services.

    Reads from environment variables and an optional ``.env`` file. Service-specific
    settings should subclass this rather than duplicating these fields.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "platform-service"
    # Accepts APP_ENV (the convention used by Dockerfile/Makefile/.env-example, matching
    # platform-gincommon) as well as the field's own default name.
    environment: str = Field(
        default="development", validation_alias=AliasChoices("APP_ENV", "environment")
    )
    log_level: str = "INFO"
    request_timeout_seconds: float = 30.0
    enable_tracing: bool = True
    enable_metrics: bool = True
    otlp_endpoint: str | None = None
