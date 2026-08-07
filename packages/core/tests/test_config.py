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
    monkeypatch.setenv("LOOM_QUERY_SHARED_SECRET", "real-query-secret")
    monkeypatch.setenv("LOOM_STORAGE_ROOT_SECRET_KEY", "real-storage-root-secret")
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


def test_default_query_shared_secret_rejected_outside_local(monkeypatch) -> None:
    """`query_shared_secret` (Task 10/11) suit CÙNG khuôn `session_secret` —
    một field mới thêm vào `_INSECURE_DEFAULTS` mà quên set biến môi trường
    tương ứng ở dev/prod phải chặn khởi động, không phải một 401 âm thầm ở
    lần request đầu tiên chạm `loom-query`."""
    monkeypatch.setenv("LOOM_ENVIRONMENT", "prod")
    monkeypatch.setenv("LOOM_SESSION_SECRET", "real-secret")
    monkeypatch.setenv("LOOM_OIDC_CLIENT_SECRET", "real-client-secret")
    with pytest.raises(ValidationError, match="query_shared_secret"):
        Settings()


def test_default_storage_root_secret_rejected_outside_local(monkeypatch) -> None:
    """Cùng khuôn `query_shared_secret` — `storage_root_secret_key` là credential
    GỐC của MinIO (xem docstring ở `Settings`), nên nó phải chặn khởi động y hệt
    một secret bị bỏ quên, không phải cho qua rồi hỏng âm thầm ở lần tạo
    lakehouse đầu tiên ở dev/prod."""
    monkeypatch.setenv("LOOM_ENVIRONMENT", "prod")
    monkeypatch.setenv("LOOM_SESSION_SECRET", "real-secret")
    monkeypatch.setenv("LOOM_OIDC_CLIENT_SECRET", "real-client-secret")
    monkeypatch.setenv("LOOM_QUERY_SHARED_SECRET", "real-query-secret")
    with pytest.raises(ValidationError, match="storage_root_secret_key"):
        Settings()


def test_default_secrets_allowed_in_local() -> None:
    settings = Settings(environment="local")
    assert settings.session_secret == "dev-only-do-not-use-in-production"
    assert settings.query_shared_secret == "dev-only-do-not-use-in-production"
    assert settings.storage_root_secret_key == "dev-only-do-not-use-in-production"


def test_public_base_url_trailing_slash_is_stripped() -> None:
    settings = Settings(public_base_url="http://loom.localhost/")
    assert settings.public_base_url == "http://loom.localhost"


def test_public_base_url_without_trailing_slash_is_unchanged() -> None:
    settings = Settings(public_base_url="http://loom.localhost")
    assert settings.public_base_url == "http://loom.localhost"


def test_oidc_issuer_trailing_slash_is_stripped() -> None:
    settings = Settings(oidc_issuer="http://loom.localhost/dex/")
    assert settings.oidc_issuer == "http://loom.localhost/dex"


def test_oidc_issuer_without_trailing_slash_is_unchanged() -> None:
    settings = Settings(oidc_issuer="http://loom.localhost/dex")
    assert settings.oidc_issuer == "http://loom.localhost/dex"


def test_oidc_internal_base_trailing_slash_is_stripped() -> None:
    settings = Settings(oidc_internal_base="http://dex.loom.svc.cluster.local:5556/")
    assert settings.oidc_internal_base == "http://dex.loom.svc.cluster.local:5556"


def test_oidc_internal_base_without_trailing_slash_is_unchanged() -> None:
    settings = Settings(oidc_internal_base="http://dex.loom.svc.cluster.local:5556")
    assert settings.oidc_internal_base == "http://dex.loom.svc.cluster.local:5556"


def test_oidc_internal_base_none_is_left_as_none() -> None:
    settings = Settings(oidc_internal_base=None)
    assert settings.oidc_internal_base is None


def test_query_base_url_trailing_slash_is_stripped() -> None:
    settings = Settings(query_base_url="http://loom-query.loom.svc.cluster.local:8000/api/v1/")
    assert settings.query_base_url == "http://loom-query.loom.svc.cluster.local:8000/api/v1"


def test_query_base_url_without_trailing_slash_is_unchanged() -> None:
    settings = Settings(query_base_url="http://loom-query.loom.svc.cluster.local:8000/api/v1")
    assert settings.query_base_url == "http://loom-query.loom.svc.cluster.local:8000/api/v1"


def test_storage_endpoint_trailing_slash_is_stripped() -> None:
    settings = Settings(storage_endpoint="http://minio.loom.svc.cluster.local:9000/")
    assert settings.storage_endpoint == "http://minio.loom.svc.cluster.local:9000"


def test_lakekeeper_url_trailing_slash_is_stripped() -> None:
    settings = Settings(lakekeeper_url="http://loom-lakekeeper.loom.svc.cluster.local:8181/")
    assert settings.lakekeeper_url == "http://loom-lakekeeper.loom.svc.cluster.local:8181"


def test_get_settings_is_cached(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("LOOM_SESSION_SECRET", "real-secret")
    monkeypatch.setenv("LOOM_OIDC_CLIENT_SECRET", "real-client-secret")
    monkeypatch.setenv("LOOM_QUERY_SHARED_SECRET", "real-query-secret")
    monkeypatch.setenv("LOOM_STORAGE_ROOT_SECRET_KEY", "real-storage-root-secret")
    monkeypatch.setenv("LOOM_ENVIRONMENT", "one")
    first = get_settings()
    monkeypatch.setenv("LOOM_ENVIRONMENT", "two")
    second = get_settings()
    assert first is second
    assert second.environment == "one"
    get_settings.cache_clear()
