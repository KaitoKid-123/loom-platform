"""Cấu hình của pod nạp — TẤT CẢ đến từ biến môi trường, không có tệp nào.

Tên biến KHÔNG được chọn ở đây: `loom_api.jobs.JobLauncher.launch` là bên ĐẶT
chúng vào pod (`LOOM_TASK_RUN_ID`, `LOOM_TASK_API_BASE_URL`,
`LOOM_TASK_SHARED_SECRET`), nên `env_prefix="LOOM_TASK_"` cộng ba tên trường của
`Settings` phải khớp CHÍNH XÁC ba tên đó. Lệch một chữ nghĩa là pod không khởi
động nổi và hàng `ingest_run` kẹt ở `pending` cho tới khi có người đọc log của
một pod đã bị TTL dọn mất.

Ba trường đó KHÔNG có mặc định, có chủ đích: một `run_id` mặc định là vô nghĩa
(nó là khoá của hàng `ingest_run` mà pod này sinh ra để phục vụ), và một
`shared_secret` mặc định là một bí mật ai cũng đoán được — cùng lập luận với
`_reject_default_secrets_outside_local` ở `loom_core.config`, chỉ khác là ở đây
KHÔNG có nhánh "local thì cho qua" nào để phải cân nhắc: pod này chỉ tồn tại
trong cụm, do control plane phóng ra.

**HAI lớp, không một lớp, và ranh giới giữa chúng là ranh giới của việc BÁO
ĐƯỢC LỖI.** `Settings` là những gì cần để NÓI với control plane;
`SourceCredentials` là những gì cần để mở NGUỒN. Gộp chung thì một Secret
connection thiếu khoá làm cả `Settings()` ném — trước khi có `IngestClient` nào
để gửi `complete(status="failed")` — và hàng `ingest_run` nằm lại ở `running`
cho tới lượt đối chiếu của Task 13. Tách ra thì đúng lỗi đó thành một run
`failed` kèm câu nói rõ biến nào còn thiếu, đúng như spec mục 7 đòi ("Secret
thiếu hoặc sai tên → run `failed`, không treo `pending` mãi").

**Credential NGUỒN không đi qua control plane** (spec 3a mục 6). `loom-api` chỉ
đặt TÊN k8s Secret của connection vào `envFrom` của Job (xem
`loom_api.ingest_service.secret_name_for`), nên mọi khoá trong Secret đó thành
biến môi trường của pod này. `SourceCredentials` vì vậy là QUY ƯỚC về tên khoá
mà Secret đó phải mang — `#<key>` trong `secret_ref` hiện chỉ là chú thích cho
người đọc, `envFrom` chiếu TOÀN BỘ Secret chứ không chọn khoá nào (đúng như
docstring `secret_name_for` đã ghi).
"""

from __future__ import annotations

import uuid

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# KHÔNG đọc `.env`: khác `loom-api`/`loom-query` (hai service có vòng phát triển
# local chạy ngoài cụm), pod này chỉ chạy trong một container do `JobLauncher`
# dựng, và một tệp `.env` lọt vào ảnh sẽ là một nguồn cấu hình thứ hai không ai
# nghĩ tới khi đọc `launch()`.
_ENV_ONLY = SettingsConfigDict(env_prefix="LOOM_TASK_", extra="ignore", frozen=True)


class Settings(BaseSettings):
    """Ba giá trị mà `JobLauncher` đặt vào pod, không hơn."""

    model_config = _ENV_ONLY

    run_id: uuid.UUID
    api_base_url: str
    shared_secret: str = Field(min_length=1)


class LakehouseSettings(BaseSettings):
    """Cách với tới LAKEHOUSE — lớp thứ BA, cùng lập luận tách như `SourceCredentials`.

    Không gộp vào `Settings`: hai giá trị này CÓ mặc định dùng được, còn ba
    trường của `Settings` thì không có và không được có. Gộp lại thì một lỗi
    chính tả trong `LOOM_TASK_CATALOG_URI` không phân biệt được với một Job thiếu
    `LOOM_TASK_RUN_ID` — hai lỗi sửa ở hai chỗ hoàn toàn khác nhau. Và như
    `SourceCredentials`, tách ra giữ nó trong vùng BÁO ĐƯỢC LỖI: `IngestClient`
    đã tồn tại trước khi đường ghi bronze được dựng, nên một catalog sai địa chỉ
    thành một run `failed` kèm lý do đọc được.

    **Mặc định là địa chỉ THẬT của chart ở tên release mặc định (`loom`), không
    phải một placeholder** — nhưng `loom.fullname` đổi theo release name, nên
    Task 15 vẫn phải truyền hai biến này TƯỜNG MINH vào env của Job, đúng cách
    `query-deployment.yaml` làm cho `LOOM_QUERY_CATALOG_URI`/`LOOM_QUERY_S3_
    ENDPOINT`. Mặc định ở đây chỉ giữ cho pod chạy được ở cụm local; nó không
    phải hợp đồng triển khai.

    `warehouse` KHÔNG có ở đây: mỗi lakehouse là một warehouse riêng, đặt tên
    theo `str(lakehouse_id)` (cùng quy ước `loom_query.runner`), nên nó tới từ
    `IngestSpec` của từng run chứ không từ môi trường của pod.
    """

    model_config = _ENV_ONLY

    catalog_uri: str = "http://loom-lakekeeper.loom.svc.cluster.local:8181/catalog"
    s3_endpoint: str = "http://minio.loom.svc.cluster.local:9000"


class ReadTuning(BaseSettings):
    """Số dòng mỗi lô ĐỌC từ nguồn — lớp thứ TƯ, và là lớp duy nhất chỉ để CHỈNH.

    Ba lớp kia trả lời "làm sao nói/mở/ghi được"; lớp này không mở thêm đường nào
    cả, nó chỉ đổi TỐC ĐỘ của đường đã có. Tách riêng vì đúng lý do đó: một giá
    trị sai ở đây không làm pod mất khả năng báo lỗi, nó chỉ làm lần nạp chậm đi
    hoặc tốn RAM hơn — một loại hỏng khác hẳn ba lớp trên, và trộn nó vào
    `SourceCredentials` sẽ biến một con số chỉnh được thành một khoá bắt buộc
    phải có trong Secret của connection.

    **Mặc định 40 000 đến từ ĐO 3, không phải từ cảm giác.** Cùng đường nạp,
    cùng nguồn, chỉ đổi số dòng/lô (0,149 GB, 50 lô ở cấu hình 10 000):

        10 000 dòng/lô -> 1,5 MB/s, RSS đỉnh 402 MiB
        40 000 dòng/lô -> 3,6 MB/s, RSS đỉnh 385 MiB   (+135% thông lượng)

    RSS đỉnh THẤP HƠN chứ không cao hơn, nên đây không phải một phép đánh đổi
    RAM lấy tốc độ ở khoảng này: commit catalog là chi phí CỐ ĐỊNH mỗi lô (~0,83s
    bất kể lô lớn cỡ nào, ĐO 3), nên lô lớn gấp bốn nghĩa là số lần commit giảm
    bốn lần trong khi phần dữ liệu sống trong RAM vẫn chỉ là MỘT lô.

    **Vì sao KHÔNG nâng tiếp — đây là trần, không phải một con số bỏ dở.** ĐO 3
    đo 100 000 dòng/lô ở RSS đỉnh 587 MiB, VƯỢT `limits.memory` 512Mi của pod nạp
    (`task.memory` trong `deploy/helm/loom/values.yaml`). Vượt limit là OOMKill,
    và một pod bị OOMKill không báo được gì cả — `main.run_reporting_the_outcome`
    nói rõ SIGKILL là một trong ba lớp trường hợp nó KHÔNG phủ — nên hàng
    `ingest_run` nằm lại ở `running` cho tới vòng đối chiếu của Task 13. Đổi một
    lần nạp chậm lấy một lần nạp chết-im-lặng là một cuộc đổi tồi.

    **Vì sao là biến môi trường chứ không phải một hằng số trong mã.** Con số
    trên chỉ đúng cho HÌNH DẠNG DÒNG đã đo; một bảng nguồn có cột rộng hơn đẩy
    bức tường RAM xuống thấp hơn 40 000, và lúc đó người vận hành cần hạ nó
    xuống ĐƯỢC mà không phải build lại ảnh — cùng lập luận với `task.memory` ở
    `values.yaml`. `JobLauncher` không đặt biến này (khác ba trường của
    `Settings`), nên mặc định ở đây là giá trị chạy thật của production.

    `gt=0`: `PostgresConnector` cũng từ chối số không dương, nhưng nó từ chối
    SAU khi DSN đã được ghép — một cấu hình vô nghĩa nên chết ở chỗ nó được đọc.
    """

    model_config = _ENV_ONLY

    batch_rows: int = Field(default=40_000, gt=0)


class SourceCredentials(BaseSettings):
    """Cách MỞ nguồn — cặp duy nhất mà `IngestSourceSpec` cố ý KHÔNG mang.

    `min_length=1` chứ không mặc định rỗng: một mật khẩu rỗng gửi tới nguồn thật
    cho ra "password authentication failed", một câu khiến người vận hành đi tìm
    lỗi ở phía Postgres trong khi lỗi thật là Secret của connection thiếu khoá.
    """

    model_config = _ENV_ONLY

    source_user: str = Field(min_length=1)
    source_password: str = Field(min_length=1)
