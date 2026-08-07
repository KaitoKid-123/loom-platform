"""Trạng thái query — MỘT `dict` trong bộ nhớ tiến trình, không Redis, không DB.

Spec v1 nói rõ v1 không có session state, và một query đang chạy không mang
theo ngữ cảnh người dùng nào cần sống sót qua một lần restart pod — người dùng
chỉ mất một câu đang chờ, y hệt bấm F5 giữa lúc tải trang. Một `dict[UUID,
QueryState]` là đủ và đúng; dựng Redis cho v1 là trả nợ trước khi có nợ.

**Hệ quả cần biết:** pod chết (crash, rolling restart, autoscale-down) làm MẤT
mọi query đang chạy hoặc đã xong nhưng chưa được `GET`. Chấp nhận được ở Giai
đoạn 2b vì không có SLA nào hứa hẹn khác, và đây là một trong những nợ được ghi
ra thay vì bị lờ đi.

Không cần khoá: FastAPI (uvicorn mặc định) chạy một event loop asyncio DUY
NHẤT, và mọi hàm dưới đây hoàn tất không có điểm `await` nào ở giữa — tức là
không có cơ hội cho một coroutine khác chen vào giữa lúc đọc và lúc ghi cùng
một khoá. Interface vẫn khai `async def` vì `QueryStore` là một `Protocol`-like
phụ thuộc được tiêm vào `app.state`, và giữ nó async để một bản cài sau (ví dụ
Redis, nếu pod-restart thật sự thành vấn đề) không phải đổi chữ ký ở chỗ gọi.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from loom_query.schemas import ColumnOut


class QueryStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class QueryState:
    status: QueryStatus = QueryStatus.running
    columns: list[ColumnOut] | None = None
    rows: list[list[object]] | None = None
    error: str | None = None
    truncated: bool | None = None
    row_count: int | None = None
    # Tham chiếu tới task nền — giữ để bookkeeping (vd. chờ nó xong lúc
    # shutdown sau này); KHÔNG phải cơ chế huỷ. `task.cancel()` chỉ dừng việc
    # CHỜ một coroutine đang `await asyncio.to_thread(...)` — nó không giết
    # được thread OS bên dưới, đã kiểm bằng thực nghiệm khi viết Task 9 (một
    # `task.cancel()` giữa chừng vẫn để thread nền chạy hết). Cơ chế huỷ thật
    # là `interrupt` bên dưới.
    task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)
    # Hàm KHÔNG đối số dừng NGAY công việc DuckDB đang chạy — bound method
    # `connection.interrupt` (xem `runner.execute`), gắn vào ngay khi
    # connection tồn tại. `QueryStore.cancel` gọi nó cùng lúc lật cờ trạng
    # thái; đây là điều làm cho huỷ THẬT SỰ dừng việc, không chỉ đổi nhãn.
    interrupt: Callable[[], None] | None = field(default=None, repr=False, compare=False)


class QueryStore:
    def __init__(self) -> None:
        self._queries: dict[uuid.UUID, QueryState] = {}

    async def create(self, query_id: uuid.UUID) -> None:
        self._queries[query_id] = QueryState()

    async def attach_task(self, query_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        # Tách khỏi `create`: task nền được `asyncio.create_task` sau khi hàng
        # đã tồn tại trong store, để `runner.execute` không bao giờ thấy
        # `store.get(query_id)` trả `None` do race giữa hai lệnh insert.
        state = self._queries.get(query_id)
        if state is not None:
            state.task = task

    async def attach_interrupt(self, query_id: uuid.UUID, interrupt: Callable[[], None]) -> None:
        """Gắn cơ chế huỷ THẬT — gọi ngay khi `runner.execute` có một
        connection DuckDB để đưa cho `cancel()` dùng sau này.

        Nếu query đã bị `cancel()` TRƯỚC KHI connection kịp tồn tại — một race
        hẹp nhưng có thật: `POST` trả `202` ngay, và `DELETE` có thể tới trước
        khi luồng nền kịp mở connection — gọi `interrupt` NGAY tại đây thay vì
        chỉ cất nó đi. Không có nhánh này, một query bị huỷ đúng vào khoảnh
        khắc đó sẽ không bao giờ được `interrupt()`, và chạy trọn vẹn dù trạng
        thái đã hiện `cancelled`.
        """
        state = self._queries.get(query_id)
        if state is None:
            return
        state.interrupt = interrupt
        if state.status == QueryStatus.cancelled:
            interrupt()

    async def get(self, query_id: uuid.UUID) -> QueryState | None:
        return self._queries.get(query_id)

    async def set_succeeded(
        self,
        query_id: uuid.UUID,
        columns: list[ColumnOut],
        rows: list[list[object]],
        *,
        truncated: bool = False,
        row_count: int | None = None,
    ) -> None:
        state = self._queries.get(query_id)
        # `state is None`: hàng đã bị dọn (chưa xảy ra ở Giai đoạn 2b — không
        # có TTL nào — nhưng hàm này không được giả định điều đó mãi đúng).
        # `status == cancelled`: người dùng đã bỏ query trước khi nó xong;
        # ghi đè kết quả lên một trạng thái đã kết thúc là đúng lỗi mà Task 9
        # (huỷ) gọi tên — "huỷ một query đã xong không được đổi kết quả", áp
        # dụng ngược: một query đã bị huỷ không được "xong lại" sau đó.
        if state is None or state.status != QueryStatus.running:
            return
        state.status = QueryStatus.succeeded
        state.columns = columns
        state.rows = rows
        state.truncated = truncated
        # `row_count` mặc định bằng `len(rows)` cho chỗ gọi chưa biết tổng số
        # dòng THẬT trước khi cắt (test cũ, không phải đường đi thật của
        # `runner.py` — nó luôn truyền `row_count` tường minh).
        state.row_count = row_count if row_count is not None else len(rows)

    async def set_failed(self, query_id: uuid.UUID, error: str) -> None:
        state = self._queries.get(query_id)
        if state is None or state.status != QueryStatus.running:
            return
        state.status = QueryStatus.failed
        state.error = error

    async def cancel(self, query_id: uuid.UUID) -> bool:
        """Đổi trạng thái sang `cancelled` VÀ dừng công việc THẬT đang chạy.

        Task 6 để lại đây một bản CHỈ lật cờ — spec Giai đoạn 2b gọi tên nó là
        chỗ dễ viết một phép kiểm mù nhất trong cả plan (`DELETE` rồi `GET`
        thấy `status == "cancelled"` xanh ngay cả khi task nền vẫn chạy trọn
        vẹn). Task 9 sửa nó: nếu có `interrupt` đã gắn (xem `attach_interrupt`
        — bound method `connection.interrupt`, gắn bởi `runner.execute` ngay
        khi connection DuckDB tồn tại), gọi nó ở đây. `connection.interrupt()`
        là hàm ĐỒNG BỘ, không chặn (đã kiểm bằng thực nghiệm: nó chỉ đặt một
        cờ ngắt phía DuckDB rồi trả về ngay — DuckDB tự kiểm cờ đó định kỳ
        trong lúc thực thi và ném `duckdb.InterruptException`), nên gọi nó
        ngay trong một hàm không có `await` này là an toàn.

        `set_succeeded`/`set_failed` vẫn từ chối GHI ĐÈ một trạng thái đã
        `cancelled` — kết quả (nếu công việc lỡ kịp xong đúng lúc bị huỷ) bị
        vứt đi thay vì hiện ra sau đó.

        Trả `False` nếu không có query nào mang id này (404 ở tầng route).
        Trả `True` cả khi query đã ở trạng thái cuối (succeeded/failed/đã
        cancelled) — huỷ một query đã xong không phải lỗi, chỉ là vô tác dụng.
        """
        state = self._queries.get(query_id)
        if state is None:
            return False
        if state.status == QueryStatus.running:
            state.status = QueryStatus.cancelled
            if state.interrupt is not None:
                state.interrupt()
        return True
