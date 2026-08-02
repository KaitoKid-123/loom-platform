"""Cấu hình dùng chung. Không phụ thuộc FastAPI hay SQLAlchemy."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOOM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "local"
    log_level: str = "INFO"

    # Database — ghép URL ở tầng API để core không phải phụ thuộc SQLAlchemy
    database_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "loom"
    db_user: str = "loom"
    db_password: str = "loom"  # noqa: S105 — giá trị mặc định cho dev, không phải secret thật

    # Session cookie
    session_secret: str = "dev-only-do-not-use-in-production"  # noqa: S105
    session_cookie_name: str = "loom_session"
    session_ttl_hours: int = 12
    cookie_secure: bool = False

    # OIDC
    oidc_issuer: str = "http://loom.localhost/dex"
    oidc_internal_base: str | None = "http://dex.loom.svc.cluster.local:5556"
    oidc_client_id: str = "loom"
    oidc_client_secret: str = "loom-dev-secret"  # noqa: S105
    oidc_redirect_url: str = "http://loom.localhost/api/v1/auth/callback"

    public_base_url: str = "http://loom.localhost"


@lru_cache
def get_settings() -> Settings:
    return Settings()
