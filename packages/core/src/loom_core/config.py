"""Cấu hình dùng chung. Không phụ thuộc FastAPI hay SQLAlchemy."""

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULTS = {
    "session_secret": "dev-only-do-not-use-in-production",
    "oidc_client_secret": "loom-dev-secret",
    "query_shared_secret": "dev-only-do-not-use-in-production",
    "ingest_shared_secret": "dev-only-do-not-use-in-production",
    "schedule_shared_secret": "dev-only-do-not-use-in-production",
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
    # NGÂN SÁCH CONNECTION, không phải một con số chọn cho đẹp. Aiven service
    # của dự án có `max_connections=20` và nó KHÔNG chỉ phục vụ Loom — đo ngày
    # 2026-08-14: Lakekeeper 7, một ứng dụng KHÁC của chủ dự án (`bi_portal`,
    # application_name "PostgreSQL JDBC Driver") 5, database `loom` 4.
    #
    # Với 5+5, riêng loom-api đã được quyền chiếm 10/20, và Lakekeeper mặc định
    # được quyền chiếm 15 nữa (đọc 10 + ghi 5). Tổng quyền của hai thành phần là
    # 25 trên một server 20 slot: cụm BỘI CHI ngay từ thiết kế, và nó không vỡ
    # lúc nghỉ mà vỡ đúng lúc một consumer MỚI xin connection đầu tiên — pod nạp.
    # Đó là cách `make smoke` trượt 13/14 ở đúng ô `/ingest`.
    #
    # 3+2 để lại chỗ cho pod nạp. Xem `packages/core/tests/test_connection_budget.py`.
    db_pool_size: int = 3
    db_max_overflow: int = 2
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
    # Ba bí mật chia sẻ — MỖI THỨ một bí mật RIÊNG: ingest, query, và schedule.
    # Mỗi bí mật chỉ mở được MỘT đường, không gộp quyền. Xem
    # `loom_core.internal_auth` cho danh sách đầy đủ và lý do.
    schedule_shared_secret: str = "dev-only-do-not-use-in-production"  # noqa: S105

    # Trần đồng thời TOÀN CỤC của pipeline (spec 3b mục 6, chốt 2) — bao nhiêu
    # `pipeline_run` được phép ở trạng thái `running` cùng lúc trên cả cụm. Đọc ở
    # `routers/internal_schedule.py`, dùng bởi `schedule_service.decide()`.
    #
    # **1 ĐẾN TỪ ĐO 7**, không phải từ suy đoán:
    # `docs/measurements/2026-08-17-phase-3b-concurrency.md`. Chỗ này trước đây là
    # hằng số `CONCURRENCY_CAP = 3` và được ghi thẳng là CHƯA ĐO.
    #
    # **RÀNG BUỘC CHẶT NHẤT KHÔNG PHẢI POOL 3+2 CỦA `loom-api`.** Giả thuyết đó đã
    # bị đo và bị bác: suốt cả phiên đo, SQLAlchemy KHÔNG báo cạn pool một lần nào
    # (`QueuePool limit ...`: 0 lần), pool cao nhất chỉ giữ 4 trên 5 chỗ, và độ trễ
    # lấy session (`/api/v1/readyz`) phẳng 235 -> 239 ms giữa K = 1 và K = 2 —
    # chênh lệch đó nhỏ hơn nhiễu giữa hai lần chạy cùng K = 1. Pipeline không mở
    # connection Aiven RIÊNG cho mỗi run; tất cả đi qua cùng pool 5 chỗ, nên số
    # connection KHÔNG lớn theo K.
    #
    # **Chỗ vỡ là RAM của container `loom-query` (384Mi).** Đọc từ chính cgroup của
    # pod: `anon` 221 MiB còn nằm lại lúc KHÔNG chạy gì (58% trần), trong khi MỘT
    # bước SQL dựng silver trên một bảng bronze 150.000 dòng tốn thêm ~105-215 MiB.
    # `221 + 215 = 436 > 384`. `memory.events` của pod đếm `oom_group_kill 8` —
    # khớp CHÍNH XÁC `restartCount` của nó, tức mọi lần restart trong ngày đo đều
    # là OOM.
    #
    # **SỬA của ĐO 8 cho hai chữ trong đoạn trên:** 221 MiB đó KHÔNG phải "lúc
    # không chạy gì" theo nghĩa nền, và nó KHÔNG phải "không reclaim được". Đo lại
    # trên cùng cgroup pod: một pod VỪA KHỞI ĐỘNG, chưa phục vụ câu nào, có `anon`
    # **92,9 MiB**. 221 MiB là phần rác đã tích lại SAU khi pod chạy vài câu quét —
    # pool PyArrow chạy trên mimalloc, và mimalloc GIỮ trang đã free thay vì trả
    # lại hệ điều hành (đo được: `pa.total_allocated_bytes()` đọc 0,0 MiB trong khi
    # RSS là 706,7 MiB). `runner._release_arrow_memory()` thu phần đó về, hạ nền
    # nghỉ từ ~555 MiB xuống ~265 MiB. Nên "sàn 58%" là một chỗ RÒ đã sửa được,
    # không phải một chi phí phải thiết kế quanh nó.
    #
    # Số đo cho ranh giới hỏng, và nó NGẪU NHIÊN chứ không phải một bức tường:
    # K = 1 chưa hỏng lần nào; K = 2 có một khối 2/2 run hỏng (`loom-query` bị
    # OOMKill giữa bước SQL) và một khối 2/2 chạy được; ba câu SQL đồng thời
    # (`probe_query_conc`) chạy được ở lần 1 và bị OOMKill ở lần 2 với ĐÚNG cùng
    # cấu hình. Một trần phải nằm DƯỚI cái mép đó, không phải trên nó.
    #
    # **Vì sao 1 chứ không 0.** Luật của phép đo là "K lớn nhất không hỏng, trừ
    # một" — K lớn nhất không hỏng là 1, nên phép trừ cho 0, và 0 nghĩa là KHÔNG
    # lịch nào chạy được nữa. Nên 1 là SÀN, và biên an toàn phải đến từ chỗ khác:
    # sửa `loom-query`, không phải hạ tiếp trần này.
    #
    # **Trần này KHÔNG chữa được `loom-query`, và đừng đọc nó như thế.** Ngay ở
    # N = 1, một người dùng bấm một câu truy vấn tương tác trong lúc bước SQL của
    # pipeline đang chạy vẫn tạo ra đúng hình dạng hai-câu-đồng-thời đã đo được là
    # chết — `/api/v1/query` không đi qua `decide()`. Trần chỉ chặn phần đóng góp
    # CỦA SCHEDULER.
    #
    # **ĐO 8 đã làm đúng phép đo mà đoạn trên đòi, và trần này VẪN là 1 — nhưng lý
    # do đã khác, và cái khác đó mới là điều đáng đọc.** Trần RAM của `loom-query`
    # lên 768Mi, `anon` không còn ratchet, và `loom-query` bây giờ CHẠY ĐƯỢC những
    # câu mà ĐO 7 đo là chết: một `SELECT count(*)` trên bronze 500.000 dòng, và
    # HAI câu quét đồng thời (K = 2, 3/3 lần). Đo ở 768Mi:
    #
    #   K = 1  đỉnh ~623 MiB   chạy được
    #   K = 2  đỉnh ~731 MiB   chạy được 3/3
    #   K = 3  đụng đúng 768,0 MiB, `oom_group_kill` +1, cả ba câu mất
    #
    # Luật "K lớn nhất không hỏng, trừ một" cho 2 - 1 = **1**. Nên con số không đổi.
    #
    # **Nhưng thứ ĐÃ đổi là lỗ mà đoạn "trần này KHÔNG chữa được" ở trên nói tới.**
    # Ở 384Mi, N = 1 không đóng được lỗi: một câu tương tác cạnh một bước SQL là
    # K = 2, và K = 2 chết. Ở 768Mi, K = 2 chạy được — nên N = 1 bây giờ THẬT SỰ
    # đủ, và trần này lần đầu tiên khớp với thứ nó hứa. Cái còn hở là K = 3, và K = 3
    # đòi HAI câu tương tác cùng lúc — một hình dạng mà không trần đồng thời nào
    # chặn được, ở bất kỳ giá trị nào.
    #
    # **Muốn nâng nó lên 2 thì phải sửa cái gì:** không phải trần RAM (nâng tiếp
    # KHÔNG mua thêm an toàn — đo được: đỉnh PHÌNH cho vừa trần, 768Mi cho đỉnh 731
    # MiB còn 896Mi cho đỉnh 870 MiB, vì mimalloc chỉ trả trang khi bị ép). Thứ phải
    # sửa là NHU CẦU: đẩy phép chiếu xuống Iceberg. Đo được, cùng bảng cùng reader,
    # thêm `selected_fields=("id",)` hạ đỉnh quét từ 332 MiB xuống 3,3 MiB — xem
    # `Lakehouse.scan` và ĐO 8 mục 5. Đó là task riêng vì nó cần phân tích cột từ
    # câu SQL, thứ `loom_sql` chưa có.
    #
    # Hai trục còn lại còn nhiều chỗ ở K đã đo: RAM node cao nhất 2317 Mi trên ngân
    # sách 4096 Mi (cộng +384 Mi của trần mới thành ~2700 Mi), và slot Aiven cao
    # nhất 11 trên 20.
    #
    # Đếm TOÀN CỤC chứ không theo từng pipeline: theo từng pipeline thì chốt này là
    # hệ quả của chốt "không tự giẫm" ngay trên nó trong `decide()` và không bao giờ
    # chặn thêm hàng nào — một cái chốt chết. Bảng `pipeline` cũ (migration 0008)
    # có cột `concurrency_cap` riêng mỗi pipeline; nó đi cùng bảng khi 0009 bỏ bảng,
    # và spec chưa bao giờ đòi một trần theo pipeline.
    #
    # Là BIẾN MÔI TRƯỜNG (`LOOM_PIPELINE_CONCURRENCY_CAP`) chứ không hằng số trong
    # mã, cùng lập luận với `ReadTuning.batch_rows` bên `loom_task.config`: con số
    # này chỉ đúng cho hình dạng bước SQL đã đo, và người vận hành gặp một
    # `loom-query` to hơn (hoặc một bước SQL nặng hơn) phải sửa được nó mà không
    # phải build lại ảnh.
    #
    # CHƯA được template trong chart — cùng chỗ đứng với `ReadTuning`/`WriteTuning`,
    # nên mặc định ở đây LÀ giá trị chạy thật của mọi môi trường, và đổi nó ở
    # dev/prod hôm nay là đặt env var trên deployment. Thêm một khoá `values.yaml`
    # là việc của lần đầu tiên có người thật sự cần một giá trị khác.
    #
    # `gt=0`: 0 nghĩa là không nhịp nào chạy được nữa, và đó không phải một cấu
    # hình — đó là tắt lịch bằng một con số trông như đã chỉnh.
    pipeline_concurrency_cap: int = Field(default=1, gt=0)

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
