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
    # Tham chiếu tới task nền — Task sau (huỷ thật sự dừng công việc) sẽ gọi
    # `task.cancel()` ở đây. Ở Giai đoạn 2b, `QueryStore.cancel` CHỈ đổi cờ
    # trạng thái (xem docstring của nó) nên trường này chưa được đọc ở đâu cả,
    # nhưng chỗ để gắn nó vào đã có sẵn — không phải thêm một trường mới rồi
    # dời dữ liệu khi Task sau tới.
    task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)


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

    async def get(self, query_id: uuid.UUID) -> QueryState | None:
        return self._queries.get(query_id)

    async def set_succeeded(
        self, query_id: uuid.UUID, columns: list[ColumnOut], rows: list[list[object]]
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

    async def set_failed(self, query_id: uuid.UUID, error: str) -> None:
        state = self._queries.get(query_id)
        if state is None or state.status != QueryStatus.running:
            return
        state.status = QueryStatus.failed
        state.error = error

    async def cancel(self, query_id: uuid.UUID) -> bool:
        """Đổi trạng thái sang `cancelled`. KHÔNG dừng công việc THẬT đang chạy.

        Dừng công việc thật (huỷ task DuckDB đang quét giữa chừng) là Task sau
        — spec Giai đoạn 2b liệt kê nó riêng vì nó là chỗ dễ viết một phép kiểm
        mù nhất trong cả plan (chỉ đọc cờ trạng thái, không quan sát được công
        việc có thật sự dừng hay không). Ở đây, task nền (`runner.execute`) vẫn
        chạy tới khi xong hoặc lỗi; `set_succeeded`/`set_failed` phía trên chỉ
        từ chối GHI ĐÈ một trạng thái `cancelled` — nên kết quả của một query
        đã bị người dùng bỏ sẽ bị ÂM THẦM vứt đi thay vì hiện ra sau đó, dù
        công việc tính nó vẫn tốn CPU cho tới khi xong.

        Trả `False` nếu không có query nào mang id này (404 ở tầng route).
        Trả `True` cả khi query đã ở trạng thái cuối (succeeded/failed/đã
        cancelled) — huỷ một query đã xong không phải lỗi, chỉ là vô tác dụng.
        """
        state = self._queries.get(query_id)
        if state is None:
            return False
        if state.status == QueryStatus.running:
            state.status = QueryStatus.cancelled
        return True
