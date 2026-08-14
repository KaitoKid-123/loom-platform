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


class WriteTuning(BaseSettings):
    """Bao nhiêu lô ĐỌC đi vào MỘT commit Iceberg — lớp thứ NĂM, cùng loại với `ReadTuning`.

    Tách khỏi `ReadTuning` chứ không thêm một trường vào đó: hai con số điều khiển
    hai đầu khác nhau của đường nạp (`batch_rows` là cỡ một lô ĐỌC, `commit_every_
    batches` là số lô mỗi lần GHI-commit) và chúng hỏng theo hai kiểu khác nhau —
    `batch_rows` quá lớn là OOMKill, `commit_every_batches` quá lớn là nhiều dòng
    TRÙNG hơn khi pod chết. Trộn chúng vào một lớp làm hai docstring khác nhau phải
    sống cạnh nhau trong một chỗ.

    **K ĐÁNH ĐỔI: số dòng TRÙNG khi pod chết  <->  thời gian commit.** Đây là câu
    phải đọc trước mọi câu khác ở đây.

    * Watermark chỉ tiến SAU một commit thật (`runner.run_incremental`), nên một
      pod chết giữa nhóm làm cả nhóm phải đọc lại — tối đa `K x batch_rows` dòng,
      và những dòng đó vào bảng bronze LẦN THỨ HAI. Hợp đồng at-least-once của
      spec mục 4 cho phép trùng; nó không cho phép mất.
    * Đổi lại, số lần commit catalog giảm K lần. ĐO 3 đo commit catalog ở 44,0%
      đồng hồ tường (42,1 s trên 97,0 s ở `batch_rows=10.000`), sàn ~0,83 s mỗi
      commit BẤT KỂ lô lớn cỡ nào. Số lần BÁO watermark cũng giảm K lần, nên việc
      "báo watermark thưa hơn" (5,4 s, 13,5% đồng hồ tường của ĐO 3) tự đạt được
      mà không cần một thay đổi riêng — spec 3d mục 3b.

    **Mặc định 5, và đây là lý do chọn 5 chứ không 1 hay 50.**

    * `add_files` giữ thời gian commit PHẲNG theo số file trong khoảng đã đo (N =
      1 / 5 / 20 -> 0,56 / 0,62 / 0,61 s — `scripts/probe_iceberg_add_files.py`),
      nên K = 5 nằm giữa khoảng đó: mỗi commit vẫn rẻ như một commit một-file, và
      không có ngoại suy nào ra ngoài vùng đã đo.
    * Với `batch_rows` mặc định 40.000, K = 5 đặt trần dòng trùng ở 200.000 dòng
      — cùng ĐỘ LỚN với một lô, không cùng độ lớn với cả lần nạp. K = 50 (một
      commit cho một lần nạp 500.000 dòng) thì trần đó là CẢ BẢNG, tức là mất hẳn
      tính chất tiến-dần mà `incremental` có và `full` thì không.
    * Phần thời gian cắt được đã gần hết ở K = 5: nó bỏ 80% số lần commit. K = 20
      chỉ bỏ thêm 15% nữa (95% so với 80%) trong khi nhân trần dòng trùng lên bốn
      lần — một cuộc đổi tồi ở phía bên kia.

    K = 1 là hành vi CHÍNH XÁC của Giai đoạn 3a (một commit và một lời báo mỗi lô)
    và nó vẫn chạy được, nên hạ về 1 là đường lùi nếu dòng trùng thành vấn đề thật.

    `gt=0`: `run_incremental` cũng từ chối số không dương, nhưng nó từ chối sau khi
    connector đã mở — một cấu hình vô nghĩa nên chết ở chỗ nó được đọc.
    """

    model_config = _ENV_ONLY

    commit_every_batches: int = Field(default=5, gt=0)


class SourceCredentials(BaseSettings):
    """Cách MỞ nguồn — cặp duy nhất mà `IngestSourceSpec` cố ý KHÔNG mang.

    `min_length=1` chứ không mặc định rỗng: một mật khẩu rỗng gửi tới nguồn thật
    cho ra "password authentication failed", một câu khiến người vận hành đi tìm
    lỗi ở phía Postgres trong khi lỗi thật là Secret của connection thiếu khoá.
    """

    model_config = _ENV_ONLY

    source_user: str = Field(min_length=1)
    source_password: str = Field(min_length=1)
