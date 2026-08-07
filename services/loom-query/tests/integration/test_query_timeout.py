"""Giới hạn 1 (Task 8) — thời gian chạy, cấu hình được qua `Settings.
query_timeout_seconds`, mặc định 120s.

**Không phải một phép kiểm mù**, dù nhìn thoáng qua giống chỗ Task 9 cảnh báo:
`execute()` (xem docstring của nó, `runner.py`) chỉ gọi `store.set_failed(...)`
SAU KHI đã `await run_task` — tức là SAU KHI luồng DuckDB nền đã thật sự dừng
(bị `connection.interrupt()` ngắt). Nếu nhánh timeout QUÊN gọi `interrupt()`
nhưng vẫn giữ `await run_task`, luồng nền sẽ chạy tới hết ~3s tự nhiên trước
khi `set_failed` được gọi — và phép đo thời gian dưới đây (đòi hỏi xong trong
< 2s, đặt timeout 0.5s) sẽ tự bắt được điều đó, y hệt cách phép kiểm huỷ ở
`test_query_cancel.py` bắt được một `cancel()` chỉ lật cờ.

**Chứng minh đỏ bắt buộc:** đặt `query_timeout_seconds` thành một giá trị rất
lớn (`3600`) cho CÙNG câu nặng, chạy lại. Phép kiểm phải ĐỎ vì query giờ
`succeeded` (chạy xong tự nhiên trong ~3s, không kịp chạm timeout) thay vì
`failed` — xem log chạy tay trong báo cáo hoàn tất task.
"""

from __future__ import annotations

import time
import uuid

import pytest

from loom_query import runner
from loom_query.config import Settings
from loom_query.store import QueryStatus, QueryStore
from loom_sql import TableRef

from ..conftest import FakeAuthz

pytestmark = pytest.mark.integration

# Cùng câu nặng và cùng hiệu chỉnh thời gian với `test_query_cancel.py`
# (~3.0-3.1s dưới `threads=2`/`memory_limit=256MB`) — xem docstring ở đó.
HEAVY_SQL = (
    "SELECT * FROM sales.orders WHERE ("
    "  SELECT count(*) FROM range(800000000) t(i) WHERE i % 999999937 = 0"
    ") > 999999999999"
)

TABLE_REFS = (TableRef(namespace="sales", name="orders"),)

# Ngắn hơn nhiều so với ~3s chạy trọn, nhưng đủ dài để không đỏ vì máy chậm
# khởi động chậm hơn dự kiến (network tới Lakekeeper thật, mở catalog...).
SHORT_TIMEOUT_SECONDS = 0.5


async def test_a_query_over_the_time_limit_is_failed_and_stopped_quickly(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
) -> None:
    assert seeded_table == "sales.orders"
    fake_authz.grant(lakehouse_id, "viewer")
    short_timeout = app_settings.model_copy(update={"query_timeout_seconds": SHORT_TIMEOUT_SECONDS})

    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)

    start = time.perf_counter()
    await runner.execute(
        query_id=query_id,
        sql=HEAVY_SQL,
        lakehouse_id=lakehouse_id,
        table_refs=TABLE_REFS,
        settings=short_timeout,
        store=store,
    )
    elapsed = time.perf_counter() - start

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.failed, state.rows
    assert state.error is not None
    assert "time limit" in state.error

    assert elapsed < 2.0, (
        f"timeout={SHORT_TIMEOUT_SECONDS}s nhưng execute() mất {elapsed:.2f}s "
        "mới trả — câu nặng (~3s chạy trọn) có vẻ vẫn chạy hết thay vì bị "
        "interrupt() ngay khi hết giờ"
    )


async def test_a_query_within_the_time_limit_succeeds_normally(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
) -> None:
    """Đối chứng: timeout không được đá cả những query bình thường."""
    fake_authz.grant(lakehouse_id, "viewer")
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)

    await runner.execute(
        query_id=query_id,
        sql="SELECT id, amount FROM sales.orders ORDER BY id",
        lakehouse_id=lakehouse_id,
        table_refs=TABLE_REFS,
        settings=app_settings,  # mặc định 120s
        store=store,
    )

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.succeeded, state.error
    assert state.rows == [[1, 10.0], [2, 20.0], [3, 30.0]]
