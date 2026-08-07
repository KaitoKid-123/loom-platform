"""Task 9 — `DELETE` phải dừng công việc THẬT đang chạy, không chỉ đổi trạng thái.

**Vì sao KHÔNG dùng `DELETE` rồi `GET` để chứng minh:** `QueryStore.cancel()`
lật `state.status = cancelled` NGAY LẬP TỨC và ĐỒNG BỘ, bất kể có cơ chế dừng
thật hay không — một bản cài chỉ lật cờ (Task 6) và bản cài THẬT (Task 9) cho
CÙNG một câu trả lời tức thì qua `GET`. Đây đúng là cái bẫy spec Giai đoạn 2b
gọi tên: "một phép kiểm gọi DELETE rồi đọc GET thấy status == cancelled sẽ
XANH với một bản cài chỉ lật cờ".

**Cách chọn ở đây:** gọi thẳng `runner.execute()` (bỏ qua HTTP) và `await`
CHÍNH cái task nền đó cho tới khi nó thật sự xong — đo THỜI GIAN THẬT của việc
chờ đó. `execute()` (xem docstring của nó) không trả lại quyền điều khiển cho
người gọi cho tới khi luồng DuckDB nền đã DỪNG (thành công, lỗi, hay bị ngắt).
Nếu huỷ chỉ lật cờ mà không gọi `connection.interrupt()`, luồng nền vẫn chạy
tới hết ~3s của câu nặng bên dưới dù `store.cancel()` đã trả `True` từ giây
thứ 0.5 — `await` đó sẽ mất ~3s thay vì ~1s, và phép kiểm dưới đây bắt được
chính xác sự khác biệt đó.

**Dữ liệu GHIM để tất định:** `range(800_000_000)` với modulo số nguyên tố
lớn, đo trên máy dùng để viết task này (`threads=2`, `memory_limit=256MB` —
đúng cấu hình `runner.py`): 3.0-3.1s ổn định qua ba lần đo liên tiếp (không
lệch quá 4%). Huỷ ở 0.5s, đòi hỏi xong trong dưới 2.0s — biên gấp ~5 lần so
với khoảng cách còn lại tới lúc chạy xong tự nhiên (~2.5s).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from loom_query import runner
from loom_query.authz import ResolvedTable
from loom_query.config import Settings
from loom_query.main import create_app
from loom_query.store import QueryStatus, QueryStore
from loom_sql import TableRef

from ..conftest import FakeAuthz, http_client

pytestmark = pytest.mark.integration

# Chạm `sales.orders` THẬT (để đường mở catalog/đăng ký view thật sự chạy),
# nhưng công việc NẶNG nằm trong một subquery vô hướng ĐỘC LẬP — không nhân số
# dòng theo `sales.orders`. Xem hiệu chỉnh thời gian trong docstring module.
HEAVY_SQL = (
    "SELECT * FROM sales.orders WHERE ("
    "  SELECT count(*) FROM range(800000000) t(i) WHERE i % 999999937 = 0"
    ") > 999999999999"
)

LIGHT_SQL = "SELECT id, amount FROM sales.orders ORDER BY id"


async def _run_in_background(
    settings: Settings, lakehouse_id: uuid.UUID, store: QueryStore, sql: str
) -> tuple[uuid.UUID, asyncio.Task[None]]:
    query_id = uuid.uuid4()
    await store.create(query_id)
    resolved_tables = (
        ResolvedTable(ref=TableRef(namespace="sales", name="orders"), lakehouse_id=lakehouse_id),
    )
    task = asyncio.create_task(
        runner.execute(
            query_id=query_id,
            sql=sql,
            resolved_tables=resolved_tables,
            settings=settings,
            store=store,
        )
    )
    await store.attach_task(query_id, task)
    return query_id, task


async def test_cancel_of_a_running_query_stops_the_background_work_early(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
) -> None:
    assert seeded_table == "sales.orders"
    fake_authz.grant(lakehouse_id, "viewer")
    store = QueryStore()

    start = time.perf_counter()
    query_id, task = await _run_in_background(app_settings, lakehouse_id, store, HEAVY_SQL)

    await asyncio.sleep(0.5)  # để chắc DuckDB đã thật sự bắt đầu chạy câu nặng
    cancelled = await store.cancel(query_id)
    assert cancelled is True

    await asyncio.wait_for(task, timeout=15.0)
    elapsed = time.perf_counter() - start

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.cancelled
    assert state.rows is None  # không có kết quả nào lọt qua sau khi bị huỷ

    assert elapsed < 2.0, (
        f"huỷ ở 0.5s nhưng task nền mất {elapsed:.2f}s mới thật sự xong — câu "
        "nặng (~3s chạy trọn) có vẻ vẫn chạy hết thay vì bị interrupt() ngay"
    )


async def test_cancelling_an_already_finished_query_does_not_change_its_result(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
) -> None:
    """Vế thứ hai bắt buộc của Task 9: huỷ một query ĐÃ XONG không lỗi, và
    không đổi kết quả — qua đúng `runner.execute` thật, không phải store giả."""
    fake_authz.grant(lakehouse_id, "viewer")
    store = QueryStore()
    query_id, task = await _run_in_background(app_settings, lakehouse_id, store, LIGHT_SQL)
    await task  # câu nhẹ — chạy xong gần như ngay lập tức

    state_before = await store.get(query_id)
    assert state_before is not None
    assert state_before.status == QueryStatus.succeeded

    cancelled = await store.cancel(query_id)
    assert cancelled is True  # không lỗi

    state_after = await store.get(query_id)
    assert state_after is not None
    assert state_after.status == QueryStatus.succeeded  # KHÔNG đổi thành cancelled
    assert state_after.rows == [[1, 10.0], [2, 20.0], [3, 30.0]]  # KHÔNG đổi kết quả


async def test_cancelling_an_unknown_query_id_over_http_is_404(fake_authz: FakeAuthz) -> None:
    """Vế thứ ba bắt buộc của Task 9 — đã có unit test riêng
    (`tests/test_query_routes.py::test_delete_unknown_query_id_is_404`), lặp
    lại ở đây để bộ test huỷ (integration) tự đứng đủ, không phải nhớ file
    khác mới đủ nghiệm thu."""
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.delete(f"/api/v1/query/{uuid.uuid4()}")
    assert response.status_code == 404
