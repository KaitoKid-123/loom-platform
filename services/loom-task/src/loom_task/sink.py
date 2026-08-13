"""Chỗ DUY NHẤT trong `loom-task` biết Apache Iceberg tồn tại — qua `Lakehouse`.

Task 11 dừng ở `SinkNotBuiltYet` và nói rõ vì sao: vòng lặp `incremental` và hợp
đồng thứ tự của nó xong trước, đường ghi thật sau. File này là đường ghi thật, và
nó phải phục vụ CẢ HAI mode vì `main._build_sink` dựng đúng một đối tượng cho một
lần chạy.

## Hai cái tên phái sinh, và vì sao chúng mang `run_id`

`full` cần hai bảng phụ: `staging` (nơi ghi trước khi tráo) và `đích_cũ` (nơi dữ
liệu cũ đứng chờ trong lúc tráo). ĐO 2 mục D4 đã đo: `rename_table` TỪ CHỐI đè
lên một tên đang tồn tại. Hệ quả trực tiếp, và nó là một lỗi tự-chặn-vĩnh-viễn
nếu đặt tên sai: một lần chạy chết giữa bước 2 và bước 4 để lại `đích_cũ`, và với
một cái tên CỐ ĐỊNH (`<đích>__old`) thì lần chạy SAU hỏng ngay ở bước 2 —
`TableAlreadyExistsError` — và mọi lần chạy sau nữa cũng vậy, cho tới khi có
người vào xoá tay. Tính năng tự khoá chính nó bằng đúng cơ chế an toàn của nó.

Hai cách chữa, và đây là cách ĐƯỢC CHỌN cùng lý do:

- **Hậu tố theo `run_id` (chọn).** Không lần chạy nào đụng tên của lần chạy
  khác, nên không có gì để dọn TRƯỚC khi bắt đầu. `job_name` đã tất định theo
  `run_id` (xem `loom_api.jobs.job_name`) nên quy ước này không mới.
- **Dọn tên sót trước khi bắt đầu (bỏ).** "Dọn" ở đây nghĩa là XOÁ DỮ LIỆU của
  một người, và thứ duy nhất để nhận diện nó là một khuôn tên — một bảng do
  người dùng tự đặt tên trùng khuôn sẽ bị xoá bởi một lần nạp mà họ không nghĩ
  là có liên quan. Đổi một lỗi tự-chặn (ồn ào, sửa được) lấy một lỗi mất dữ liệu
  (im lặng, không sửa được) là đổi sai hướng.

**Cái giá của hậu tố `run_id`, nói thẳng:** một lần chạy đứt để lại rác MÃI MÃI
— bảng staging của nó, và có thể cả `đích_cũ` — vì không lần chạy nào sau đó
nhận ra chúng là rác. Chúng không chặn gì, nhưng chúng tốn đĩa, và 2c đã chứng
minh `drop_table` không xoá object dưới S3. Dọn chúng là việc tường minh, thuộc
nợ ở spec mục 13, và chưa có mã nào trong 3a làm việc đó.

**Cái giá KHÔNG được nhầm với một lợi ích:** hậu tố theo `run_id` nghĩa là một
lần nạp `full` KHÔNG nạp lại được từ chỗ đứt. Kế hoạch Task 12 có một câu nói
ngược ("`full` giờ nạp lại được từ chỗ đứt"), và nó không đúng ở bản cài đặt này
— xem docstring `staging_table_name` cho ba lý do đo được.

## `staging` nằm CÙNG namespace với đích

`bronze.x__y` cho ra `bronze.x__y__staging_<hex>`, không phải `staging.x__y`. Hai
lý do:

1. ĐO 2 mục D chỉ đo `rename_table` TRONG một namespace. Đổi tên qua namespace
   khác là một hành vi CHƯA ĐO của Lakekeeper, và cả Task 12 dựa trên việc
   `rename` làm đúng điều đã đo.
2. `target_exists()` chỉ được gọi SAU `staging_done()`, nên namespace `bronze`
   chắc chắn đã tồn tại lúc đó (chính `stage()` vừa tạo nó). Nếu staging nằm ở
   namespace khác, lần nạp đầu tiên sẽ hỏi `exists()` về một bảng trong một
   namespace chưa hề tồn tại — một trường hợp không ai cần phải dò.
"""

from __future__ import annotations

import uuid

import pyarrow as pa  # type: ignore[import-untyped]

from loom_iceberg import Lakehouse


class NothingStaged(RuntimeError):
    """`full` không ghi được lô nào, nên không có gì để tráo vào bảng đích.

    Hỏng ồn ào thay vì hai lựa chọn tệ hơn. Bỏ qua cú tráo và báo `succeeded`
    nói rằng "đã thay cả bảng" trong khi bảng vẫn là dữ liệu cũ — một lời nói dối
    đúng vào cột trạng thái mà người dùng tin. Còn tráo một bảng staging không
    tồn tại là `NoSuchTableError` từ PyIceberg, một câu không nhắc gì tới việc
    nguồn đọc ra rỗng.

    Nguồn RỖNG THẬT (bảng nguồn không có dòng nào) rơi vào đây, và đó là một hạn
    chế chứ không phải hành vi mong muốn: đúng ra `full` nên thay bảng đích bằng
    một bảng rỗng. Làm được thì phải dựng bảng staging từ schema của
    `discover()` chứ không từ lô đầu tiên — việc thật, chưa làm, và một lần nạp
    `failed` kèm lý do đọc được là chỗ dừng an toàn cho tới lúc làm.
    """


def _namespace_of(qualified: str) -> str:
    """Phần namespace của một tên đầy đủ — `rpartition`, KHÔNG `partition`.

    Namespace của Iceberg nhiều tầng được (`a.b.c` = namespace `a.b`, bảng `c`),
    nên cắt ở dấu chấm ĐẦU sẽ trả về `a` và tạo một namespace khác cái mà bảng
    thật sự nằm trong.
    """
    namespace, _, _ = qualified.rpartition(".")
    if not namespace:
        raise ValueError(f"tên bảng phải có namespace, nhận {qualified!r}")
    return namespace


def staging_table_name(target: str, run_id: uuid.UUID) -> str:
    """Bảng tạm của MỘT lần chạy. Hậu tố `run_id` — xem docstring module.

    **Vì sao hậu tố này KHÔNG làm `full` nạp lại được**, dù staging commit từng
    lô. Ba sự thật đo được, cộng lại:

    1. `JobLauncher` dựng Job với `backoff_limit=0` (xem `jobs.py`), nên
       Kubernetes KHÔNG BAO GIỜ chạy lại pod của một run đã chết. "Cùng một run
       nạp tiếp" không có đường nào xảy ra.
    2. Bấm Nạp lần nữa tạo một hàng `ingest_run` MỚI với `run_id` mới, nên nó
       tính ra một tên staging khác và bắt đầu từ bảng rỗng.
    3. Kể cả khi tên staging cố định giữa hai lần chạy, `full` KHÔNG có watermark
       (spec mục 5, và `ingest_spec` không gửi cursor cho mode này) — lần chạy
       sau không có cách nào biết staging đã có những lô nào, nên nó đọc lại
       nguồn từ dòng đầu và NỐI vào phần cũ. Bảng staging nhân đôi, rồi được
       tráo vào làm bảng đích. Mất dữ liệu thì không, nhưng số liệu sai gấp đôi
       thì có, và im lặng.

    Nên `run_id` trong tên không phải thứ CHẶN việc nạp lại — nó chỉ nói ra sự
    thật rằng 3a không có cơ chế nạp lại cho `full`. Điều commit-từng-lô mua
    được là ĐO ĐƯỢC: RAM có chặn theo lô (ĐO 1/ĐO 2), và bảng đích không bị chạm
    tới cho tới khi staging ghi xong.

    `run_id.hex` chứ không `str(run_id)`: dấu gạch ngang trong một UUID biến tên
    bảng thành một định danh phải trích dẫn trong mọi câu SQL chạm tới nó (kể cả
    một câu SELECT tay của người đi dọn rác), và tên bảng bronze quanh nó thì
    không cần.
    """
    return f"{target}__staging_{run_id.hex}"


def old_target_name(target: str, run_id: uuid.UUID) -> str:
    """Nơi dữ liệu CŨ đứng chờ giữa bước 2 và bước 4 của cú tráo.

    Cùng hậu tố `run_id` với `staging_table_name`, cùng một lý do (xem docstring
    module): một cái tên cố định làm lần chạy sau hỏng ở bước 2 vĩnh viễn.
    """
    return f"{target}__old_{run_id.hex}"


class IcebergSink:
    """`Sink` thật (xem `loom_task.runner.Sink`) trên một `Lakehouse` Iceberg.

    Giữ `Lakehouse` chứ không `RestCatalog`: mọi thứ ngoài `packages/icebergkit`
    nói chuyện bằng Apache Arrow, và đó là điều làm việc đổi engine ở spec v1 mục
    5.9 khả thi thay vì chỉ là một lời hứa.
    """

    def __init__(self, lakehouse: Lakehouse, *, target: str, run_id: uuid.UUID) -> None:
        self._lakehouse = lakehouse
        self._target = target
        self._staging = staging_table_name(target, run_id)
        self._old_target = old_target_name(target, run_id)
        # Những tên mà lần chạy NÀY đã xác nhận tồn tại. Sau lô đầu, câu hỏi
        # "bảng này có chưa" đã có câu trả lời chắc chắn (chính ta vừa ghi vào
        # nó), nên hỏi lại catalog mỗi lô là một round trip cho một sự thật
        # không đổi được.
        self._existing: set[str] = set()

    def append(self, batch: pa.RecordBatch) -> None:
        """`incremental`: ghi VÀ commit vào bảng ĐÍCH."""
        self._write(self._target, batch)

    def stage(self, batch: pa.RecordBatch) -> None:
        """`full`: ghi VÀ commit vào bảng STAGING. Đích không bị chạm."""
        self._write(self._staging, batch)

    def _write(self, qualified: str, batch: pa.RecordBatch) -> None:
        """Tạo bảng ở lô đầu, nối thêm ở các lô sau — MỘT commit mỗi lô.

        `create_from` cho lô đầu vì bảng bronze CHƯA TỒN TẠI ở lần nạp đầu tiên
        của một stream, và schema Iceberg của nó phải sinh ra từ schema Arrow
        của chính dữ liệu (kèm ba cột `BRONZE_COLUMNS` mà `add_bronze_columns`
        vừa thêm) — không có nguồn schema nào khác đúng hơn.

        `create_namespace_if_not_exists` TRƯỚC `exists()`: hỏi một catalog xem
        một bảng có tồn tại trong một namespace chưa hề tồn tại là một trường
        hợp biên không cần thiết phải dò, và namespace `bronze` phải được tạo ở
        đây dù sao — lần nạp đầu tiên của một lakehouse mới không có gì cả.
        """
        data = pa.Table.from_batches([batch])
        if qualified not in self._existing:
            self._lakehouse.create_namespace_if_not_exists(_namespace_of(qualified))
            self._existing.add(qualified)
            if not self._lakehouse.exists(qualified):
                self._lakehouse.create_from(qualified, data)
                return
        self._lakehouse.append(qualified, data)

    def staging_done(self) -> None:
        """Không có bảng staging thì KHÔNG tráo — xem `NothingStaged`."""
        if not self._lakehouse.exists(self._staging):
            raise NothingStaged(
                f"mode 'full' đọc nguồn ra 0 lô nên bảng tạm {self._staging!r} chưa "
                f"bao giờ được tạo — {self._target!r} KHÔNG bị thay đổi. Nếu bảng "
                "nguồn thật sự rỗng, đây là hạn chế đã biết của Giai đoạn 3a"
            )

    def target_exists(self) -> bool:
        """Lần nạp `full` đầu tiên của một lakehouse trả về `False` ở đây."""
        return self._lakehouse.exists(self._target)

    def rename_target_away(self) -> None:
        self._lakehouse.rename_table(self._target, self._old_target)

    def promote_staging(self) -> None:
        self._lakehouse.rename_table(self._staging, self._target)

    def drop_old_target(self) -> None:
        """Bỏ TÊN `đích_cũ` khỏi catalog — object trên S3 vẫn còn.

        2c đã đo điều này trên Lakekeeper thật (xem `Lakehouse.drop_table`), nên
        bước này KHÔNG giải phóng đĩa và không có chỗ nào được nói là nó có.
        """
        self._lakehouse.drop_table(self._old_target)
