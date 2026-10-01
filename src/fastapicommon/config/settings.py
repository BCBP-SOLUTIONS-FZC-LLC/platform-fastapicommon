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

    # --- Enterprise Platform Observability Standard (mirrors gincommon.Config) ---
    # Registry domain (iam, workflow, billing, ...) — env OBSERVABILITY_DOMAIN. Required whenever
    # enable_metrics is true: the platform_* metrics carry the const labels
    # {domain, service, environment}; `service` is app_name and `environment` is the explicitly
    # provided `environment` (APP_ENV / ENVIRONMENT / kwarg; the "development" default does not
    # count). create_platform_app raises ValueError on a missing or unknown domain, an app_name
    # that is not a valid service label, or a missing / out-of-vocabulary environment.
    observability_domain: str = ""
    # Build version — env BUILD_VERSION; "unknown" when empty. Only platform_build_info carries
    # it (as its `version` label).
    build_version: str = ""
