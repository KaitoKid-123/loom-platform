"""Cấu hình `loom-scheduler` — RIÊNG với `loom_core.config.Settings` của loom-api.

Cùng lập luận (và cùng khuôn) với `loom_query.config`: `Settings` dùng chung
mang theo OIDC/session/**database**, mà database là đúng thứ service này tồn
tại để KHÔNG có (xem `tests/test_no_db_no_k8s.py`). Kế thừa nó sẽ kéo
`loom_core.config` — và cùng với nó là mọi trường kết nối Postgres — vào một
tiến trình mà tính chất bán được của nó là "0 connection slot Aiven".

Tiền tố biến môi trường `LOOM_SCHEDULER_*` — không trùng `LOOM_*` của loom-api
lẫn `LOOM_QUERY_*` của loom-query, nên ba pod triển khai cạnh nhau không đọc
nhầm biến của nhau.
"""

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bản sao CỦA CHÍNH service này của `loom_core.config._INSECURE_DEFAULTS` —
# cùng lý do đã ghi ở `loom_query.config`: không kế thừa `Settings` dùng chung
# thì không kế thừa được phép kiểm này. Giá trị PHẢI khớp
# `loom_core.config.Settings.schedule_shared_secret`: hai bên gửi/kiểm cùng một
# bí mật, và cùng một chuỗi "mặc định không an toàn" ở CẢ HAI phía là điều kiện
# để mỗi service tự từ chối khởi động khi biến môi trường bị quên — thay vì hai
# bên gọi nhau rồi 401 ở lần tick đầu tiên trong production.
_INSECURE_DEFAULTS = {
    "shared_secret": "dev-only-do-not-use-in-production",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOOM_SCHEDULER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: str = "local"
    log_level: str = "INFO"

    # GỐC của loom-api, KHÔNG kèm `/internal/schedule` — `ticker.tick_url()` là
    # chỗ DUY NHẤT biết đường dẫn đó, cùng cách `loom_task.client` ghép đường
    # `/internal/ingest/*` từ `LOOM_TASK_API_BASE_URL`. Để phần đường dẫn trong
    # cấu hình nghĩa là một lần đổi route bên loom-api phải sửa cả values.yaml
    # của chart, và một chuỗi sai ở đó chỉ lộ ra dưới dạng 404 mỗi N giây.
    #
    # Mặc định là địa chỉ THẬT của chart ở release name mặc định, nhưng
    # `loom.fullname` đổi theo release nên `scheduler-deployment.yaml` vẫn
    # truyền biến này TƯỜNG MINH — mặc định ở đây chỉ để chạy được ngoài cụm.
    api_base_url: str = "http://loom-api.loom.svc.cluster.local:8000"

    # Bí mật của ĐƯỜNG NÀY, không dùng lại `query-shared-secret` hay
    # `ingest-shared-secret`: ai cầm nó thì khởi động được scheduled pipeline
    # run — xem `loom_core.internal_auth` cho cả ba quyền tách biệt.
    shared_secret: str = "dev-only-do-not-use-in-production"  # noqa: S105

    # Nhịp. 30 giây là con số CHỌN, không phải đo: một tick không làm việc gì
    # ngoài một request HTTP, còn `decide()` bên loom-api chốt nhịp cron theo
    # `scheduled_for` (ràng buộc UNIQUE `(pipeline_id, scheduled_for)`), nên
    # tick dày hơn KHÔNG sinh thêm run — nó chỉ rút ngắn ĐỘ TRỄ giữa lúc một
    # nhịp cron tới hạn và lúc run được tạo, và độ trễ giữa hai NẤC của một
    # chuỗi (tick vừa khởi động vừa đối chiếu — xem docstring
    # `loom_api.routers.internal_schedule`). Nhịp nhỏ nhất mà lịch cron của
    # Loom biểu diễn được là một phút, nên 30s cho mỗi nhịp cron ít nhất một
    # tick, với một tick dự phòng nếu một lần nào đó lỗi.
    tick_seconds: float = Field(default=30.0, gt=0)

    # Trần của backoff — xem `ticker._delay_seconds`. Có trần chứ không nhân
    # đôi mãi: một `loom-api` sập nửa giờ rồi sống lại KHÔNG được kéo theo một
    # scheduler còn ngủ hàng giờ sau đó, vì mọi nhịp cron trong khoảng ngủ đó
    # đều trôi qua mà không ai xử lý.
    max_backoff_seconds: float = Field(default=300.0, gt=0)

    # `httpx` KHÔNG có timeout mặc định cho toàn bộ request nếu không truyền
    # tham số — cùng cạm bẫy đã ghi ở `loom_task.client`. Không có nó thì một
    # `loom-api` treo (chứ không sập) giữ luôn vòng lặp này đứng im vô hạn, và
    # triệu chứng là "scheduler không làm gì cả" mà không một dòng log nào.
    #
    # LỚN HƠN `TICK_BUDGET_SECONDS` của endpoint tick: bên đó tự cắt ngắn phần
    # việc để trả nhanh, nên một timeout ngắn hơn ngân sách của nó sẽ cắt đúng
    # những tick ĐANG làm việc.
    request_timeout_seconds: float = Field(default=60.0, gt=0)

    @field_validator("api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # Cùng cạm bẫy đã ghi ở `loom_core.config` và `loom_query.config`: một
        # dấu / thừa biến phép nối `f"{base}/internal/..."` thành hai gạch chéo
        # liền nhau, và Starlette trả 404 cho đường dẫn đó.
        return value.rstrip("/")

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
                f"when LOOM_SCHEDULER_ENVIRONMENT={self.environment!r}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
