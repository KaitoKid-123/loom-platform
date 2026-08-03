"""Cấu hình dùng chung. Không phụ thuộc FastAPI hay SQLAlchemy."""

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULTS = {
    "session_secret": "dev-only-do-not-use-in-production",
    "oidc_client_secret": "loom-dev-secret",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOOM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
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
    db_pool_size: int = 5
    db_max_overflow: int = 5

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

    @field_validator("public_base_url", "oidc_issuer", "oidc_internal_base")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        """Một dấu / thừa ở cuối làm hỏng phép kiểm biên trong _to_internal:
        `startswith(public + "/")` sẽ thành so sánh hai gạch chéo và không khớp
        cả URL hợp lệ, tắt câm split-horizon mà không báo lỗi gì."""
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def _reject_default_secrets_outside_local(self) -> "Settings":
        if self.environment == "local":
            return self
        still_default = [
            name for name, insecure in _INSECURE_DEFAULTS.items() if getattr(self, name) == insecure
        ]
        if still_default:
            raise ValueError(
                f"phải đặt giá trị thật cho {', '.join(sorted(still_default))} "
                f"khi LOOM_ENVIRONMENT={self.environment!r}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
