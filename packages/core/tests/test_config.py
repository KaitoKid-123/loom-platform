from loom_core.config import Settings, get_settings


def test_settings_uses_defaults() -> None:
    settings = Settings()
    assert settings.environment == "local"
    assert settings.db_name == "loom"
    assert settings.oidc_client_id == "loom"


def test_settings_reads_prefixed_env(monkeypatch) -> None:
    monkeypatch.setenv("LOOM_ENVIRONMENT", "dev")
    monkeypatch.setenv("LOOM_DB_PASSWORD", "s3cr3t")
    monkeypatch.setenv("LOOM_DB_PORT", "6543")
    settings = Settings()
    assert settings.environment == "dev"
    assert settings.db_password == "s3cr3t"
    assert settings.db_port == 6543


def test_get_settings_is_cached(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("LOOM_ENVIRONMENT", "one")
    first = get_settings()
    monkeypatch.setenv("LOOM_ENVIRONMENT", "two")
    second = get_settings()
    assert first is second
    assert second.environment == "one"
    get_settings.cache_clear()
