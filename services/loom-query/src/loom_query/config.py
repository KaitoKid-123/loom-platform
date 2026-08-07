"""Cấu hình `loom-query` — RIÊNG với `loom_core.config.Settings` của loom-api.

Không mở rộng `Settings` dùng chung: nó mang theo OIDC/session/database, mọi
thứ mà một service không có database và không tự xác thực ai (xem docstring
`main.py`) không cần biết tới. Hai Settings đọc cùng tiền tố biến môi trường
`LOOM_QUERY_*` — không trùng `LOOM_*` của loom-api — nên hai pod triển khai
cạnh nhau không đọc nhầm biến của nhau.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOOM_QUERY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: str = "local"
    log_level: str = "INFO"

    # Gốc của router `internal` bên loom-api — xem `authz.py`. KHÔNG `/api/v1`:
    # đó là router mà ingress không chuyển tới từ ngoài cluster
    # (`loom_api.routers.internal`), và là đường DUY NHẤT loom-query được hỏi
    # quyền qua đó.
    authz_base_url: str = "http://loom-api.loom.svc.cluster.local/internal"

    # Lakekeeper — MỘT catalog REST cho cả cụm ở Giai đoạn 2b. Mỗi lakehouse là
    # một WAREHOUSE riêng trong catalog đó, đặt tên theo `item.id` (xem
    # `runner.py`) — KHÔNG theo `item.name`, vì tên đổi được còn id thì không.
    catalog_uri: str = "http://lakekeeper.loom.svc.cluster.local:8181/catalog"
    s3_endpoint: str = "http://minio.loom.svc.cluster.local:9000"

    @field_validator("authz_base_url", "catalog_uri", "s3_endpoint")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # Cùng cạm bẫy đã ghi ở `loom_core.config`: một dấu / thừa làm hỏng
        # phép nối chuỗi `f"{base}/authz/items"` thành hai gạch chéo liền nhau.
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
