"""Ba route của `loom-query`: `POST` tạo, `GET` đọc trạng thái, `DELETE` huỷ.

Bất đồng bộ có chủ đích (spec Giai đoạn 2b): một câu SELECT quét nhiều phút
giữ một kết nối HTTP nhiều phút là cách chắc chắn dính timeout của MỌI proxy
trên đường (ingress, load balancer, trình duyệt). `POST` vì vậy chỉ chạy cổng
quyền — rẻ, không chạm S3, xem `authz.run_gate` — rồi trả `202` kèm `query_id`
NGAY; việc quét THẬT chạy trong một task nền (`runner.execute`), và `GET` là
nơi client polling để lấy kết quả.

Đúng như `authz.run_gate` yêu cầu: `run_gate` PHẢI chạy xong, ĐỒNG BỘ, trước
khi hàm này trả `202`. Không có `await asyncio.sleep(0)` hay `create_task` nào
chen vào giữa — nếu có, một client polling đủ nhanh có thể thấy `query_id` của
một query mà cổng quyền chưa kịp chặn.

**Nợ đã biết, ghi ra thay vì lờ đi:** `GET`/`DELETE` nhận một `query_id` (UUID
sinh ngẫu nhiên phía server, không đoán được) nhưng KHÔNG kiểm lại principal
nào đang gọi — chưa có khái niệm "principal nào tạo ra query nào" ngoài đúng
lúc `POST` chạy cổng quyền. Ở Giai đoạn 2b, đây là mức bảo vệ chấp nhận được
(id không đoán được, và bộ nhớ chỉ sống trong một request-response ngắn của
một phiên làm việc), nhưng KHÔNG phải bất biến vĩnh viễn: nếu `GET`/`DELETE`
sau này cần phân biệt "ai được xem kết quả của ai", chỗ này phải thêm một
cổng quyền thứ hai.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request, status

from loom_query import runner
from loom_query.authz import AuthzPort, LakehouseResolver, run_gate
from loom_query.config import Settings
from loom_query.schemas import QueryCreate, QueryCreated, QueryStatusOut
from loom_query.store import QueryStore

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryCreated, status_code=status.HTTP_202_ACCEPTED)
async def create_query(body: QueryCreate, request: Request) -> QueryCreated:
    authz: AuthzPort = request.app.state.authz
    resolver: LakehouseResolver = request.app.state.resolver
    resolved_tables = await run_gate(
        sql=body.sql,
        lakehouse_id=body.lakehouse_id,
        workspace_id=body.workspace_id,
        principal=body.principal,
        authz=authz,
        resolver=resolver,
    )

    store: QueryStore = request.app.state.store
    settings: Settings = request.app.state.settings

    query_id = uuid.uuid4()
    # Tạo hàng TRƯỚC khi lập lịch task nền: `runner.execute` ghi kết quả bằng
    # `store.set_succeeded`/`set_failed`, và cả hai đều bỏ qua im lặng nếu hàng
    # chưa tồn tại (xem docstring `store.py`) — đảo thứ tự hai dòng dưới đây là
    # một race hiếm nhưng có thật giữa "task bắt đầu chạy" và "hàng được tạo".
    await store.create(query_id)
    task = asyncio.create_task(
        runner.execute(
            query_id=query_id,
            sql=body.sql,
            resolved_tables=resolved_tables,
            settings=settings,
            store=store,
        )
    )
    await store.attach_task(query_id, task)
    return QueryCreated(query_id=query_id)


@router.get(
    "/query/{query_id}",
    response_model=QueryStatusOut,
    response_model_exclude_none=True,
)
async def get_query(query_id: uuid.UUID, request: Request) -> QueryStatusOut:
    store: QueryStore = request.app.state.store
    state = await store.get(query_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no query with this id")
    return QueryStatusOut(
        status=str(state.status),
        columns=state.columns,
        rows=state.rows,
        error=state.error,
        truncated=state.truncated,
        row_count=state.row_count,
    )


@router.delete("/query/{query_id}", status_code=status.HTTP_202_ACCEPTED)
async def cancel_query(query_id: uuid.UUID, request: Request) -> dict[str, str]:
    store: QueryStore = request.app.state.store
    cancelled = await store.cancel(query_id)
    if not cancelled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no query with this id")
    state = await store.get(query_id)
    assert state is not None  # `cancel()` vừa trả True cho đúng id này
    return {"status": str(state.status)}
