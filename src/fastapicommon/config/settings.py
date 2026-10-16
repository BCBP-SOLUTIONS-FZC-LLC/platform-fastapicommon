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
    # APP_ENV, then ENVIRONMENT (platform-gincommon's precedence). One of the registry's
    # local, dev, test, staging, prod; empty when unset (production log / trace defaults, and
    # create_platform_app rejects it while metrics are enabled).
    environment: str = Field(default="", validation_alias=AliasChoices("APP_ENV", "environment"))
    # LOG_LEVEL (debug, info, warn, error); empty: debug in dev / local, info otherwise (Go).
    log_level: str = ""
    request_timeout_seconds: float = 30.0
    # Proxies whose X-Forwarded-For / X-Real-IP are believed for the client IP (Gin
    # SetTrustedProxies): comma-separated IPs or CIDRs, e.g. "10.0.0.0/8,127.0.0.1". Empty (the
    # default): the client IP is the socket peer, so clients cannot spoof it. Env
    # TRUSTED_PROXIES; an invalid entry fails create_platform_app.
    trusted_proxies: str = ""
    enable_tracing: bool = True
    enable_metrics: bool = True
    # Overrides OTEL_EXPORTER_OTLP_ENDPOINT (default localhost:4317). "none" / "off" /
    # "disabled", like OTEL_TRACES_EXPORTER=none, disables export. Env OTLP_ENDPOINT.
    otlp_endpoint: str | None = None
    # RFC 9457 type URI base ({base}/{domain}/{error}) — env PROBLEM_TYPE_BASE; empty means
    # https://errors.company.com (gincommon Config.ProblemTypeBase).
    problem_type_base: str = ""

    # --- Enterprise Platform Observability Standard (mirrors gincommon.Config) ---
    # Registry domain (iam, workflow, billing, ...) — env OBSERVABILITY_DOMAIN. Required whenever
    # enable_metrics is true: the platform_* metrics carry the const labels
    # {domain, service, environment}; `service` is app_name and `environment` is `environment`
    # (APP_ENV / ENVIRONMENT / kwarg). create_platform_app raises ValueError on a missing or
    # unknown domain, an app_name that is not a valid service label, or a missing /
    # out-of-vocabulary environment.
    observability_domain: str = ""
    # Build version — env BUILD_VERSION; "unknown" when empty. Only platform_build_info carries
    # it (as its `version` label).
    build_version: str = ""
