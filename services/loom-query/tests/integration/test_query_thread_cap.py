"""Giới hạn 5 (Task 8) — `threads=2` GHIM CỨNG, xác nhận nó còn sau Task 6.

Đo đạc gốc: `packages/icebergkit/tests/test_duckdb_memory.py` (Giai đoạn 2a).
File đó kiểm ở tầng DuckDB thuần; file này kiểm ở tầng SERVICE — qua đúng
`runner.execute()` sẽ chạy trong production, mở catalog Iceberg thật, đăng ký
view thật — vì `_THREADS` là một hằng cục bộ trong `runner.py`, không phải
một tham số lộ ra ngoài để test tầng DuckDB thuần kiểm hộ được.

**Gọi thẳng `runner.execute`, không qua HTTP/`run_gate`.** Thăm dò trong lúc
viết Task 8: `loom_sql.table_deps("... FROM range(4000000) ...")` trả về MỘT
`TableRef(namespace=None, name='')` giả cho lời gọi hàm bảng `range(...)` —
sqlglot đọc nó như một `exp.Table` không tên. Đi qua `run_gate` thật, cái
`TableRef` giả đó bị `_resolve_item_id` từ chối bằng 400 "không có namespace"
TRƯỚC khi chạm tới `runner` — không liên quan gì tới giới hạn tài nguyên đang
kiểm ở đây (đây là một hạn chế có sẵn của `sqlkit.table_deps` với hàm bảng,
không phải lỗi của Task 8/9 — ghi lại, không phải việc của task này để sửa).
Gọi thẳng `runner.execute` với `table_refs` tự dựng né được hạn chế đó, và
vẫn kiểm ĐÚNG thứ cần kiểm: cấu hình DuckDB mà `_run_sync` thật sự dùng.

Dữ liệu GHIM đúng theo phép đo 2a (4.000.000 dòng, mỗi dòng có một chuỗi biến
thiên ~512 byte — `repeat(md5(...))`, KHÔNG lặp lại, để không bị nén từ điển
làm phép đo vô nghĩa, xem docstring `test_duckdb_memory.py`): threads=2 chạy
xong, threads=4 (hoặc mặc định theo số core — máy CI/dev thường ≥4 core) OOM.

**Chứng minh đỏ 2 của Task 8** (bắt buộc): xoá dòng
`connection.execute(f"SET threads={_THREADS}")` khỏi `runner._run_sync`, rồi
chạy lại `test_the_pinned_thread_count_lets_a_heavy_query_finish`. Trên một
máy nhiều core (đã kiểm trên máy 8 core dùng để viết task này), DuckDB dùng
mặc định = số core, và phép kiểm phải ĐỎ vì query OOM thay vì `succeeded`. Ghi
log chạy tay đó vào báo cáo hoàn tất task, rồi phục hồi dòng đã xoá.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from loom_query import runner
from loom_query.config import Settings
from loom_query.store import QueryStatus, QueryStore
from loom_sql import TableRef

from ..conftest import FakeAuthz

pytestmark = pytest.mark.integration

# ~512B/dòng, KHÁC NHAU từng dòng — xem docstring module. Hai subquery vô
# hướng ĐỘC LẬP (không CROSS JOIN) để không nhân số dòng: một subquery nặng
# (sắp xếp 4M dòng biến thiên — chạm bộ nhớ), một subquery chạm `sales.orders`
# thật (để việc mở catalog/đăng ký view thật sự chạy qua đúng `_run_sync`).
HEAVY_SORT_SQL = (
    "SELECT "
    "(SELECT count(*) FROM ("
    "  SELECT i, repeat(md5(i::VARCHAR), 16) AS pad FROM range(4000000) t(i) ORDER BY pad, i"
    ")) AS heavy_count, "
    "(SELECT count(*) FROM sales.orders) AS orders_count"
)


async def test_the_pinned_thread_count_lets_a_heavy_query_finish(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
) -> None:
    """Baseline XANH của Task 8/giới hạn 5: `256MB` + `threads=2` GHIM trong
    `runner.py` phải để một query 4M-dòng-sắp-xếp chạy xong KHÔNG OOM — đúng
    con số 2a bàn giao. Nếu ai đó gỡ `SET threads=2`, xem docstring module để
    tái tạo chứng minh đỏ."""
    assert seeded_table == "sales.orders"
    fake_authz.grant(lakehouse_id, "viewer")
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)

    await asyncio.wait_for(
        runner.execute(
            query_id=query_id,
            sql=HEAVY_SORT_SQL,
            lakehouse_id=lakehouse_id,
            table_refs=(TableRef(namespace="sales", name="orders"),),
            settings=app_settings,
            store=store,
        ),
        timeout=60.0,
    )

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.succeeded, state.error
    assert state.rows == [[4_000_000, 3]]
