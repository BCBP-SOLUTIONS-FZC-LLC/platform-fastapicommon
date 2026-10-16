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
    # LOG_LEVEL (debug, info, warn, error); empty: debug in dev / local, info otherwise (Go).
    log_level: str = ""
    request_timeout_seconds: float = 30.0
    enable_tracing: bool = True
    enable_metrics: bool = True
    # Overrides OTEL_EXPORTER_OTLP_ENDPOINT (default localhost:4317; "none" disables export).
    # Env OTLP_ENDPOINT.
    otlp_endpoint: str | None = None
    # RFC 9457 type URI base ({base}/{domain}/{error}) — env PROBLEM_TYPE_BASE; empty means
    # https://errors.company.com (gincommon Config.ProblemTypeBase).
    problem_type_base: str = ""

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
