"""Cấu hình dùng chung. Không phụ thuộc FastAPI hay SQLAlchemy."""

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULTS = {
    "session_secret": "dev-only-do-not-use-in-production",
    "oidc_client_secret": "loom-dev-secret",
    "query_shared_secret": "dev-only-do-not-use-in-production",
    "ingest_shared_secret": "dev-only-do-not-use-in-production",
    "storage_root_secret_key": "dev-only-do-not-use-in-production",
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
    # asyncpg hiểu `ssl`, không phải `sslmode` (cách libpq/Aiven viết) — xem
    # `_normalise_ssl_param` ở loom_api.db. `verify-full` là mặc định an toàn:
    # kết nối managed Postgres qua Internet công cộng cần xác thực server,
    # không chỉ mã hoá.
    db_sslmode: str = "verify-full"

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

    # `loom-query` (Task 10/11) — chuyển tiếp `/api/v1/query*` sang service này.
    # Namespace "loom" viết cứng, cùng quy ước với `oidc_internal_base` ở trên
    # và `storage.endpoint`/`lakekeeper` bên `deploy/helm/loom/values.yaml`: chart
    # cố tình bỏ qua `.Release.Name` khi đặt tên service (xem `_helpers.tpl`
    # `loom.fullname`), nên DNS nội bộ luôn cố định là `<service>.loom.svc.
    # cluster.local` bất kể tên release.
    query_base_url: str = "http://loom-query.loom.svc.cluster.local:8000/api/v1"
    # Bí mật chia sẻ qua header `X-Loom-Query-Secret` (xem `loom_core.
    # internal_auth`) — chứng minh với `loom-query` rằng request tới TỪ
    # loom-api, không phải từ một pod bất kỳ trong namespace tự xưng principal
    # bất kỳ. Cùng khuôn `session_secret`: mặc định KHÔNG an toàn, và
    # `_reject_default_secrets_outside_local` dưới đây từ chối khởi động nếu
    # nó còn là mặc định khi `environment != "local"`.
    query_shared_secret: str = "dev-only-do-not-use-in-production"  # noqa: S105

    # Storage — nền cho warehouse Lakekeeper của item `lakehouse` (khoảng trống
    # Giai đoạn 2a phát hiện: tạo item mà không tạo warehouse thì `GET
    # /catalog/v1/config` trả 400 lúc mở nó). Cùng khuôn DNS nội bộ "loom" viết
    # cứng như `query_base_url` ở trên.
    storage_endpoint: str = "http://minio.loom.svc.cluster.local:9000"
    storage_bucket: str = "loom-local"
    # Credential GỐC của MinIO — MÓN NỢ đã được chủ dự án chấp nhận, có điều
    # kiện: CHỈ `loom_api.warehouse_provisioning` được đọc hai trường này, để
    # tự cấp phát warehouse lúc tạo lakehouse. Giai đoạn 1 xây `loom-api` như
    # một control plane KHÔNG đọc secret nào (`connection` chỉ giữ `secret_ref`
    # — xem `SECRET_REF_RE` ở `loom_core.item_definitions`); đây là một
    # credential mở được MỌI prefix của MỌI workspace, không riêng một
    # workspace nào, nên phạm vi đọc nó phải hẹp nhất có thể và phải CANH
    # ĐƯỢC — xem `services/api/tests/test_root_credential_guard.py`, phép canh
    # AST khẳng định đúng MỘT module tham chiếu hai trường này.
    storage_root_access_key: str = "loom-dev-minio-root"
    storage_root_secret_key: str = "dev-only-do-not-use-in-production"  # noqa: S105
    # Lakekeeper — base URL, KHÔNG có hậu tố `/catalog`: loom-api chỉ gọi API
    # QUẢN TRỊ (`/management/v1/...`) để cấp phát warehouse, không bao giờ mở
    # catalog Iceberg (đó là việc riêng của `loom-query` — xem
    # `loom_query.config.Settings.catalog_uri`, field đó CÓ hậu tố `/catalog`).
    lakekeeper_url: str = "http://loom-lakekeeper.loom.svc.cluster.local:8181"

    # Task nạp (Giai đoạn 3a) — cấu hình cho `loom_api.jobs.JobLauncher`, module
    # DUY NHẤT được `import kubernetes` (xem `services/api/tests/
    # test_k8s_client_guard.py`). Cùng khuôn DNS nội bộ "loom" viết cứng như
    # `query_base_url`/`lakekeeper_url` ở trên.
    # `loom/task:dev`, cùng khuôn `loom/api`/`loom/web`/`loom/query` — ba ảnh kia
    # dùng dấu GẠCH CHÉO (xem `deploy/helm/loom/values.yaml` và các target
    # `build-*` trong Makefile). Task 7 viết `loom-task:dev` khi chưa có
    # Dockerfile nào để đối chiếu; đổi ở đây để tag mà Tilt nạp vào node
    # (`loom-task-image` trong Tiltfile) và tag mà Job yêu cầu là CÙNG một chuỗi.
    # Lệch nhau thì pod nạp `ImagePullBackOff` đi hỏi docker.io, và hàng
    # `ingest_run` kẹt ở `pending`.
    task_image: str = "loom/task:dev"
    task_namespace: str = "loom"
    task_cpu: str = "50m"
    # `limits.memory` cho pod nạp — 512Mi, TRÊN đỉnh RSS 421 MiB đã đo thật cho
    # hình dạng lô 200k dòng/lô, 20 lô, còn đang bò lên ở lô cuối (xem mục 8 của
    # docs/superpowers/specs/2026-08-11-loom-phase-3a-ingest-design.md, "ĐO 1 —
    # KẾT QUẢ CUỐI"). Con số này CHỈ đúng cho hình dạng lô đã đo — lô lớn hơn
    # hoặc dòng rộng hơn (nhiều cột hơn, cột kiểu lớn hơn) đòi đo lại, không
    # suy ra được từ con số này.
    task_memory: str = "512Mi"
    task_api_base_url: str = "http://loom-api.loom.svc.cluster.local:8000"
    # ĐỊA CHỈ của bí mật chia sẻ trong cụm — TÊN Secret và KHOÁ, KHÔNG phải giá
    # trị. `loom-api` chỉ chuyển cặp này vào `secretKeyRef` của pod nạp (xem
    # `JobLauncher.launch`); nó không đọc nội dung ở ĐƯỜNG ĐÓ.
    #
    # Mặc định `loom-app` chỉ ĐÚNG cho một lần install chart với
    # `oidc.existingSecret` để trống (local) — đó là lúc `secret.yaml` được
    # render, dưới tên `<fullname>-app`. Dev/prod đặt
    # `oidc.existingSecret: loom-app-secrets` và khi đó `secret.yaml` KHÔNG
    # render chút nào, nên `api-deployment.yaml` PHẢI truyền
    # `LOOM_TASK_SHARED_SECRET_NAME={{ include "loom.appSecretName" . }}` (nó
    # có truyền — cùng biểu thức với `LOOM_QUERY_SHARED_SECRET` ngay trên đó).
    # Thiếu dòng ấy, Job nạp hỏi một Secret không tồn tại: pod kẹt ở
    # `CreateContainerConfigError` còn hàng `ingest_run` kẹt ở `pending`.
    task_shared_secret_name: str = "loom-app"  # noqa: S105 — tên Secret, không phải giá trị
    # Khoá RIÊNG, KHÔNG dùng lại `query-shared-secret`. Task 9 tạm trỏ vào khoá
    # đó để pod khởi động được và ghi món nợ lại ngay chỗ này; Task 10 trả nó,
    # vì đây là lúc nó bắt đầu có giá.
    #
    # `loom-query` KHÔNG có OIDC: nó nhận principal của người dùng cuối ngay
    # trong thân request và tin nguyên (xem `loom_query.security` và
    # `loom_query.routers.query`), gác cửa DUY NHẤT là bí mật chia sẻ. Ai cầm
    # giá trị đó thì giả được `loom-api` với `loom-query` DƯỚI DANH NGHĨA BẤT
    # KỲ PRINCIPAL NÀO — vượt qua trọn vẹn mô hình RBAC. Mà pod nạp lại đúng là
    # thành phần quay số ra một `ConnectionDefinition.host` do NGƯỜI DÙNG nhập,
    # tức là bề mặt bị chiếm dễ nhất trong cả cụm. Cho nó một bí mật thứ hai,
    # chỉ mở được `/internal/ingest/*`, giữ một pod nạp bị chiếm ở lại trong
    # phạm vi nạp dữ liệu.
    task_shared_secret_key: str = "ingest-shared-secret"  # noqa: S105 — tên khoá
    # GIÁ TRỊ của cùng bí mật đó — cặp `task_shared_secret_name`/`_key` ở trên
    # là ĐỊA CHỈ, cái này là NỘI DUNG, và `loom-api` cần cả hai vì nó đứng ở cả
    # hai đầu: nó bảo pod đọc bí mật ở đâu (địa chỉ, vào `secretKeyRef`), rồi
    # nó KIỂM header pod gửi lên (nội dung, xem `loom_api.internal_security`).
    # `loom-query` có đúng một bản tương đương của trường này —
    # `loom_query.config.Settings.shared_secret` — vì nó chỉ đứng ở đầu kiểm.
    #
    # Env var `LOOM_INGEST_SHARED_SECRET`, nạp từ CÙNG khoá Secret mà
    # `task_shared_secret_key` trỏ tới (`api-deployment.yaml` giữ hai dòng đó
    # cạnh nhau). Lệch nhau nghĩa là pod gửi một bí mật còn API kiểm một bí mật
    # khác: mọi lần nạp 401 và hàng `ingest_run` kẹt ở `running`.
    #
    # Ở `_INSECURE_DEFAULTS`, cùng khuôn `session_secret`: dev/prod quên đặt nó
    # thì `loom-api` TỪ CHỐI KHỞI ĐỘNG, chứ không chạy với một bí mật ai cũng
    # đoán được.
    ingest_shared_secret: str = "dev-only-do-not-use-in-production"  # noqa: S105

    @field_validator(
        "public_base_url",
        "oidc_issuer",
        "oidc_internal_base",
        "query_base_url",
        "storage_endpoint",
        "lakekeeper_url",
    )
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
                f"set a real value for {', '.join(sorted(still_default))} "
                f"when LOOM_ENVIRONMENT={self.environment!r}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
