import pytest

from fastapicommon.config import PlatformSettings


def test_default_settings() -> None:
    settings = PlatformSettings(_env_file=None)
    assert settings.app_name == "platform-service"
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.request_timeout_seconds == 30.0
    assert settings.enable_tracing is True
    assert settings.enable_metrics is True
    assert settings.otlp_endpoint is None


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "my-service")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("ENABLE_TRACING", "false")

    settings = PlatformSettings(_env_file=None)
    assert settings.app_name == "my-service"
    assert settings.request_timeout_seconds == 5.0
    assert settings.enable_tracing is False


def test_settings_ignores_unknown_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_UNRELATED_VAR", "value")
    settings = PlatformSettings(_env_file=None)
    assert settings.app_name == "platform-service"


def test_environment_accepts_app_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    settings = PlatformSettings(_env_file=None)
    assert settings.environment == "prod"


def test_environment_still_accepts_its_own_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "local")
    settings = PlatformSettings(_env_file=None)
    assert settings.environment == "local"
