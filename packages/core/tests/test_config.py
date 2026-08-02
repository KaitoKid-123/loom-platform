import pytest
from pydantic import ValidationError

from loom_core.config import Settings, get_settings


def test_settings_uses_defaults() -> None:
    settings = Settings()
    assert settings.environment == "local"
    assert settings.db_name == "loom"
    assert settings.oidc_client_id == "loom"


def test_settings_reads_prefixed_env(monkeypatch) -> None:
    monkeypatch.setenv("LOOM_ENVIRONMENT", "dev")
    monkeypatch.setenv("LOOM_SESSION_SECRET", "real-secret")
    monkeypatch.setenv("LOOM_OIDC_CLIENT_SECRET", "real-client-secret")
    monkeypatch.setenv("LOOM_DB_PASSWORD", "s3cr3t")
    monkeypatch.setenv("LOOM_DB_PORT", "6543")
    settings = Settings()
    assert settings.environment == "dev"
    assert settings.db_password == "s3cr3t"
    assert settings.db_port == 6543


def test_default_secrets_rejected_outside_local(monkeypatch) -> None:
    monkeypatch.setenv("LOOM_ENVIRONMENT", "prod")
    with pytest.raises(ValidationError, match="session_secret"):
        Settings()


def test_default_secrets_allowed_in_local() -> None:
    settings = Settings(environment="local")
    assert settings.session_secret == "dev-only-do-not-use-in-production"


def test_get_settings_is_cached(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("LOOM_SESSION_SECRET", "real-secret")
    monkeypatch.setenv("LOOM_OIDC_CLIENT_SECRET", "real-client-secret")
    monkeypatch.setenv("LOOM_ENVIRONMENT", "one")
    first = get_settings()
    monkeypatch.setenv("LOOM_ENVIRONMENT", "two")
    second = get_settings()
    assert first is second
    assert second.environment == "one"
    get_settings.cache_clear()
