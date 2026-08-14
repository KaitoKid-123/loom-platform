"""Test double cho `Sink` và `IngestClient`.

**`from doubles import ...` chứ KHÔNG `from .doubles import ...`, và đừng "sửa"
lại.** Thư mục này CỐ Ý không có `__init__.py`: `services/loom-query/tests` đã là
một package tên `tests`, nên một package thứ hai cùng tên làm `import
tests.test_runner_incremental` phân giải vào thư mục của loom-query và cả HAI
module test ở đây hỏng lúc collect (`ModuleNotFoundError: No module named
'tests.test_runner_incremental'` — đã dựng lại thật, không phải suy luận). Không
có `__init__.py` thì pytest thêm chính thư mục này vào `sys.path`, và `doubles`
là một module cấp cao — đúng khuôn `services/api/tests` và
`packages/connectorkit/tests`, hai thư mục test cũng không phải package.

**Không dùng `unittest.mock`.** Một `MagicMock` chấp nhận MỌI lời gọi — kể cả sai
tên, sai thứ tự, sai số lượng — nên nó không canh được hợp đồng nào; và hợp đồng
mà Task 11 tồn tại để khoá CHÍNH LÀ thứ tự. Những lớp dưới đây ghi lại đúng thứ
tự sự kiện vào MỘT list dùng chung, nên "ghi rồi báo" và "báo rồi ghi" là hai
chuỗi khác nhau đọc được bằng mắt.

**Hai chỗ dưới đây gọi CHÍNH mã thật thay vì mô tả lại luật của server**, và cả
hai là có chủ đích: `moves_forward` cho phép tiến watermark, và
`IngestCompletionReport` cho cặp `status`/`error`. Một double lệch khỏi bản thật
làm mọi test dùng nó chứng minh về một hệ thống không tồn tại — bản nháp kế hoạch
so CHUỖI ở đây kèm chú thích tự nhận là "giống hệt luật ở phía server", và với
`initial_cursor="100"` cùng 300 dòng thì các mốc báo về là `"99"`, `"199"`,
`"299"`, mà so chuỗi cho `"199" > "99"` là `False`: double sẽ TỪ CHỐI đúng những
lần tiến hợp lệ, và `test_resume_starts_from_the_reported_watermark` sẽ canh một
hành vi không ai có.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pyarrow as pa  # type: ignore[import-untyped]

from loom_connector import StreamState
from loom_core.cursor import moves_forward
from loom_core.schemas import IngestCompletionReport
from loom_task.runner import Boom


class RecordingSink:
    """Ghi lại `(tên sự kiện, số dòng)` theo đúng thứ tự, vào list dùng chung.

    Tên sự kiện TRÙNG tên phương thức của `Sink` cho cả bảy bước — xem docstring
    `Sink`.

    **`commit` là một sự kiện RIÊNG, và đó là chỗ hợp đồng của Giai đoạn 3d được
    canh.** `write` chỉ ghi file Parquet; dữ liệu chỉ bền sau `commit`. Nếu hai
    việc đó cùng để lại một tên sự kiện thì chuỗi của bản ĐÚNG ("ghi, commit,
    báo") và bản SAI ("ghi, báo, commit" — watermark tiến qua dữ liệu chưa bền,
    tức mất dòng im lặng) không phân biệt được, và bài canh thứ tự xanh cho cả
    hai.

    **`stage` ghi `"stage"`, KHÔNG `"write"`, và khác biệt đó là cả điểm của
    double này.** Đột biến nguy hiểm nhất của Task 12 là "bỏ staging, ghi thẳng
    vào bảng đích": nếu hai đường ghi cùng để lại một tên sự kiện, chuỗi sự kiện
    của bản đúng và bản sai giống nhau từng phần tử, và mọi bài test thứ tự ở
    đây xanh cho một bản cài đặt xoá bảng của người dùng.

    `has_target` mô phỏng câu trả lời của `target_exists()`: `True` là lakehouse
    đã có bảng đích (chuỗi ba bước), `False` là lần nạp `full` ĐẦU TIÊN (không
    có gì để đổi tên đi, không có gì để xoá).
    """

    def __init__(
        self,
        events: list[tuple[str, int]],
        *,
        has_target: bool = True,
        columns: list[str] | None = None,
    ) -> None:
        self.events = events
        self.has_target = has_target
        self.columns = columns

    def write(self, batch: pa.RecordBatch) -> None:
        self.events.append(("write", batch.num_rows))

    def commit(self) -> None:
        self.events.append(("commit", 0))

    def stage(self, batch: pa.RecordBatch) -> None:
        self.events.append(("stage", batch.num_rows))

    def staging_done(self) -> None:
        self.events.append(("staging_done", 0))

    def target_exists(self) -> bool:
        # KHÔNG ghi sự kiện — một câu hỏi, không phải một thao tác. Ghi nó vào
        # `events` sẽ làm `kinds[-3:]` của bài canh thứ tự đọc ra một chuỗi khác
        # tuỳ theo bản cài đặt hỏi lúc nào, tức là biến một phép canh về THỨ TỰ
        # TRÁO thành một phép canh về thứ tự đặt câu hỏi.
        return self.has_target

    def target_columns(self) -> list[str] | None:
        """Cột của bảng đích, hoặc `None` khi bảng chưa tồn tại.

        KHÔNG thuộc Protocol `Sink` (xem docstring `IcebergSink.target_columns`)
        — chỉ `main.ingest` gọi, và chỉ ở đường `incremental`. Cũng KHÔNG ghi sự
        kiện, cùng lý do với `target_exists`: một câu hỏi, không phải thao tác.

        `None` mặc định vì đó là câu trả lời cho lần nạp ĐẦU của một stream, và
        là hình dạng mà mọi bài dùng double này trước Task 15 ngầm giả định.
        """
        return self.columns

    def rename_target_away(self) -> None:
        self.events.append(("rename_target_away", 0))

    def promote_staging(self) -> None:
        self.events.append(("promote_staging", 0))

    def drop_old_target(self) -> None:
        self.events.append(("drop_old_target", 0))


class CollectingSink:
    """Giữ lại các `id` đã COMMIT, để khẳng định TẬP HỢP dòng sau khi nạp lại.

    Giữ `list` chứ không `set`: câu hỏi "có mất dòng nào không" cần tập hợp,
    nhưng câu hỏi "at-least-once có thật sinh trùng không" cần số lần xuất hiện,
    và hai bài crash trong `test_runner_incremental` khẳng định cả hai.

    **`write` KHÔNG đưa dòng vào `ids`; chỉ `commit` mới.** Đây là điểm quan trọng
    nhất của double này từ Giai đoạn 3d, và nó là mô hình đúng của đường thật: một
    file Parquet đã ghi mà chưa `add_files` nằm trên S3 và KHÔNG người đọc nào của
    bảng bronze thấy nó. Một double gom ngay lúc `write` sẽ đếm những dòng vô hình
    đó là "đã hạ cánh", nên nó sẽ báo XANH cho đúng lỗi mà bài crash tồn tại để
    bắt — một nhóm không bao giờ được commit.

    `die_at_commit` mô phỏng pod chết ĐÚNG LÚC commit — chỗ đứt duy nhất phân biệt
    được "commit rồi báo" với "báo rồi commit" bằng DỮ LIỆU thay vì bằng thứ tự sự
    kiện. `crash_after_batch` của `run_incremental` không tới được chỗ đó: nó ném
    sau khi cả lô (kể cả cả nhóm) đã xong, nên với nó hai thứ tự cho ra cùng một
    kết quả.
    """

    def __init__(self, *, die_at_commit: int | None = None) -> None:
        self.ids: list[int] = []
        self.die_at_commit = die_at_commit
        self.commits = 0
        self._pending: list[int] = []

    def write(self, batch: pa.RecordBatch) -> None:
        self._pending.extend(batch.column("id").to_pylist())

    def commit(self) -> None:
        self.commits += 1
        if self.commits == self.die_at_commit:
            # Bỏ luôn phần chưa commit: tiến trình vừa chết, nên những file Parquet
            # đó thành object mồ côi trên S3 mà không lần chạy nào sau đó nhìn tới.
            # Giữ chúng lại để một `commit()` sau cứu là mô phỏng một thế giới nơi
            # pod sống lại cùng bộ đệm của nó — không phải thế giới này.
            self._pending.clear()
            raise Boom("pod chết đúng lúc commit")
        self.ids.extend(self._pending)
        self._pending.clear()


@dataclass
class RecordingClient:
    """`IngestClient` giả. `initial_cursor` mô phỏng watermark đã có ở control plane.

    Nó KHÔNG mô phỏng HTTP: `IngestClient` thật đã được đọc từng dòng cạnh
    `routers/internal_ingest.py`, còn thứ ba bài test của Task 11 hỏi là thứ tự
    và khả năng nạp lại — hai tính chất không dính gì tới lớp truyền tải.
    """

    events: list[tuple[str, int]]
    initial_cursor: str | None = None
    cursor_column: str = "id"
    source_id: str = "conn-1"
    progress_calls: list[tuple[str, str, str, int]] = field(default_factory=list)
    status: str | None = None
    error: str | None = None

    def current_state(self) -> StreamState:
        if self.initial_cursor is None:
            return StreamState()
        return StreamState(cursor_column=self.cursor_column, cursor_value=self.initial_cursor)

    def report_progress(
        self,
        *,
        rows: int,
        cursor_column: str | None = None,
        cursor_type: str | None = None,
        cursor_value: str | None = None,
    ) -> None:
        self.events.append(("progress", rows))
        self.progress_calls.append((cursor_column, cursor_type, cursor_value, rows))
        # Một lời báo KHÔNG mang cursor (đường `full`) không được đụng watermark —
        # đúng như `/progress` bên server: nó chỉ gọi `_advance_watermark` khi
        # `cursor_column` VÀ `cursor_type` đều có. Bỏ nhánh này thì double sẽ
        # `moves_forward(None, ...)` và nổ, che mất chính tính chất đang canh.
        if cursor_column is None or cursor_type is None or cursor_value is None:
            return
        # Gọi CHÍNH `moves_forward` mà `_advance_watermark` gọi — không viết lại
        # luật (xem docstring module).
        if self.initial_cursor is None or moves_forward(
            cursor_type, self.initial_cursor, cursor_value
        ):
            self.cursor_column = cursor_column
            self.initial_cursor = cursor_value

    def complete(self, *, status: str, error: str | None = None) -> None:
        # Dựng model THẬT: `error` bắt buộc khi `failed`, bị cấm khi `succeeded`.
        # Một `main.py` báo thành công kèm lý do hỏng (hoặc báo hỏng mà không nói
        # vì sao) sẽ nhận 422 từ `loom-api` — ở đây nó nhận `ValidationError`
        # trong test, cùng luật, sớm hơn.
        IngestCompletionReport.model_validate({"status": status, "error": error})
        self.status = status
        self.error = error
        self.events.append(("complete", 0))
