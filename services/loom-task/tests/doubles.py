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


class RecordingSink:
    """Ghi lại `("write", số dòng)` theo đúng thứ tự, vào list dùng chung."""

    def __init__(self, events: list[tuple[str, int]]) -> None:
        self.events = events

    def append(self, batch: pa.RecordBatch) -> None:
        self.events.append(("write", batch.num_rows))


class CollectingSink:
    """Giữ lại các `id` đã ghi, để khẳng định TẬP HỢP dòng sau khi nạp lại.

    Giữ `list` chứ không `set`: câu hỏi "có mất dòng nào không" cần tập hợp,
    nhưng câu hỏi "at-least-once có thật sinh trùng không" cần số lần xuất hiện,
    và bài `test_a_crash_mid_run_loses_no_rows` khẳng định cả hai.
    """

    def __init__(self) -> None:
        self.ids: list[int] = []

    def append(self, batch: pa.RecordBatch) -> None:
        self.ids.extend(batch.column("id").to_pylist())


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
        self, *, cursor_column: str, cursor_type: str, cursor_value: str, rows: int
    ) -> None:
        self.events.append(("progress", rows))
        self.progress_calls.append((cursor_column, cursor_type, cursor_value, rows))
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
