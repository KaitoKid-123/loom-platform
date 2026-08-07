"""Cấu hình `loom-query` — RIÊNG với `loom_core.config.Settings` của loom-api.

Không mở rộng `Settings` dùng chung: nó mang theo OIDC/session/database, mọi
thứ mà một service không có database và không tự xác thực ai (xem docstring
`main.py`) không cần biết tới. Hai Settings đọc cùng tiền tố biến môi trường
`LOOM_QUERY_*` — không trùng `LOOM_*` của loom-api — nên hai pod triển khai
cạnh nhau không đọc nhầm biến của nhau.
"""

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cùng khuôn với `loom_core.config._INSECURE_DEFAULTS`/
# `_reject_default_secrets_outside_local` (Task 2) — `loom-query` không mở
# rộng `Settings` dùng chung (xem docstring module) nên nó cần bản sao CỦA
# CHÍNH NÓ của phép kiểm này, không phải kế thừa được. Giá trị insecure PHẢI
# khớp `Settings.query_shared_secret` bên `loom_core.config`: hai bên gửi/kiểm
# cùng một bí mật, và cùng một chuỗi "mặc định không an toàn" cho cả hai phía
# là điều kiện để `_reject_default_secrets_outside_local` của MỖI service tự
# đứng vững — dev/prod quên set biến môi trường ở BÊN NÀO cũng khiến chính
# service đó từ chối khởi động, không đợi tới lúc hai bên gọi nhau rồi 401.
_INSECURE_DEFAULTS = {"shared_secret": "dev-only-do-not-use-in-production"}


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

    # Ba trong năm giới hạn tài nguyên (Task 8) — CẤU HÌNH ĐƯỢC theo yêu cầu
    # ràng buộc của Giai đoạn 2b, khác `memory_limit`/`threads` DuckDB (GHIM
    # CỨNG trong `runner.py`, xem docstring ở đó: hai giá trị đó là kết quả đo
    # đạc của Giai đoạn 2a, không phải một tham số vận hành nên KHÔNG lộ ra
    # đây — lộ chúng ra biến môi trường là mời một lần chỉnh tay làm trôi mất
    # phép đo).
    query_timeout_seconds: float = Field(default=120.0, gt=0)
    max_scan_bytes: int = Field(default=10 * 1024**3, gt=0)  # 10 GiB
    max_result_rows: int = Field(default=10_000, gt=0)

    # Bí mật chia sẻ qua header (Task 10/11) — xem `loom_query.security` và
    # `loom_core.internal_auth`. Env var `LOOM_QUERY_SHARED_SECRET` — TRÙNG
    # với biến mà `loom_core.config.Settings.query_shared_secret` đọc bên
    # loom-api (prefix `LOOM_` + tên trường `query_shared_secret`), cố ý: MỘT
    # khoá Secret Kubernetes nạp vào CẢ HAI Deployment (xem `deploy/helm/loom/
    # templates/secret.yaml` và `query-deployment.yaml`/`api-deployment.yaml`)
    # thay vì hai khoá phải giữ đồng bộ tay.
    shared_secret: str = "dev-only-do-not-use-in-production"  # noqa: S105

    @field_validator("authz_base_url", "catalog_uri", "s3_endpoint")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # Cùng cạm bẫy đã ghi ở `loom_core.config`: một dấu / thừa làm hỏng
        # phép nối chuỗi `f"{base}/authz/items"` thành hai gạch chéo liền nhau.
        return value.rstrip("/")

    @model_validator(mode="after")
    def _reject_default_secrets_outside_local(self) -> "Settings":
        # Bản sao CỦA CHÍNH `loom-query` của khuôn ở `loom_core.config` — xem
        # comment trên `_INSECURE_DEFAULTS` cho lý do không kế thừa được.
        if self.environment == "local":
            return self
        still_default = [
            name for name, insecure in _INSECURE_DEFAULTS.items() if getattr(self, name) == insecure
        ]
        if still_default:
            raise ValueError(
                f"set a real value for {', '.join(sorted(still_default))} "
                f"when LOOM_QUERY_ENVIRONMENT={self.environment!r}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
