"""Chuyển tiếp `/api/v1/query*` sang `loom-query` — Task 10/11.

Trình duyệt chỉ nói chuyện với `loom-api` (Giai đoạn 1 chỉ hứa MỘT mặt xác
thực: cookie phiên httpOnly, không token nào lộ ra trình duyệt). `loom-query`
chạy sau nó, ClusterIP, không qua ingress (xem `deploy/helm/loom/templates/
ingress.yaml` — chỉ hai path, `/api` và `/`, không path nào cho `loom-query`)
— router này là cách DUY NHẤT một request từ trình duyệt chạm tới nó.

**Chuyển tiếp RẺ vì API phía dưới bất đồng bộ** (xem docstring
`loom_query.routers.query`): `POST` trả `202` kèm `query_id` NGAY, không giữ
kết nối, nên một round trip HTTP bình thường (`httpx.AsyncClient.request`) là
đủ — KHÔNG dựng streaming proxy. `GET`/`DELETE` cũng vậy.

Ba route đều dùng chung `_forward`: chuyển tiếp status code VÀ thân phản hồi
NGUYÊN VẸN từ `loom-query` (không đi qua `install_error_handlers`/
`ProblemDetail` của `loom-api` — trả một `Response` trực tiếp bỏ qua tầng
đó). `loom-query` KHÔNG có OIDC/session riêng nên body lỗi của nó (400 kèm
dòng/cột SQL, 403 "thiếu quyền"...) đã đúng hình dạng client cần thấy; dịch
lại nó qua một schema thứ hai chỉ để đổi vỏ là việc không có giá trị.

**`workspace_id`: BẮT BUỘC tự tra, KHÔNG BAO GIỜ dùng giá trị client gửi.**
`loom-api` có database — nó biết `lakehouse_id` (một `Item` loại `lakehouse`)
thuộc workspace nào — nên đây là nơi DUY NHẤT có đủ thẩm quyền đó;
`loom-query` không có database (xem docstring `loom_query.main`) và không tự
tra được. Nếu để giá trị client tự khai đi qua, một client gửi `workspace_id`
của workspace KHÁC sẽ khiến bước phân giải tên bảng ba phần
(`lakehouse.ns.table`, xem `loom_query.authz._resolve_tables`) chạy SAI PHẠM
VI — người dùng phân giải được tên lakehouse ở một workspace mà họ không có
quyền, và cổng quyền viewer chạy SAU bước phân giải đó nên không cứu được:
tên tồn tại hay không đã bị lộ trước khi có ai được hỏi quyền.

Tra cứu `_lakehouse_workspace_id` CỐ Ý KHÔNG kiểm quyền `item_read` trên
`lakehouse_id` — cùng lý do `POST /internal/lakehouses/resolve` không kiểm
quyền (xem docstring `routers/internal.py`): cổng quyền THẬT xảy ra ngay sau,
bên trong `loom-query` (`run_gate`, hỏi `POST /internal/authz/items` với
CHÍNH `lakehouse_id` này luôn có mặt trong tập id — xem bất biến bắt buộc ở
`loom_query.authz.run_gate`). Thêm một cổng quyền THỨ HAI ở đây là tính lại
một phần luật RBAC mà differential test của `permissions.py` sinh ra để
chặn trôi — `lakehouse_id` không tồn tại (hoặc không phải type `lakehouse`,
hoặc đã xoá mềm) chỉ đơn thuần là 404 ở ĐÂY, trước khi tốn một round trip
sang `loom-query` cho một thứ chắc chắn thất bại.

**Bí mật chia sẻ** (`X-Loom-Query-Secret`, xem `loom_core.internal_auth` và
`loom_query.security`): đính kèm ở MỌI request gửi sang `loom-query`, chứng
minh nó tới TỪ `loom-api` — nơi duy nhất biết bí mật — chứ không phải từ một
pod bất kỳ trong namespace tự xưng principal bất kỳ (xem docstring
`loom_core.internal_auth` cho khoảng hở mà nó đóng, và nợ nó KHÔNG đóng).

**`GET /lakehouses/{lakehouse_id}/schema` (Task 2, Giai đoạn 2c) CỐ Ý KHÔNG
tra `_lakehouse_workspace_id` trước khi chuyển tiếp — khác BA route ở trên.**
Route đó cần `workspace_id` THẬT để phân giải tên bảng ba phần, và 404 sớm ở
đó là một đánh đổi đã ghi nhận (rò rỉ sự tồn tại của `lakehouse_id`, chấp nhận
được vì SQL người dùng gõ vào đã cần biết id đó tồn tại). Route schema thì
KHÁC: spec bắt buộc "lakehouse không tồn tại" và "không có quyền" phải cho ra
CÙNG một 403, không phân biệt được — nên route này CHUYỂN TIẾP VÔ ĐIỀU KIỆN,
không tra database gì cả, và để `loom-query` (`run_schema_gate`, hỏi
`/internal/authz/items` với CHÍNH `lakehouse_id`) quyết định. `loom-api` không
tự biết `lakehouse_id` có tồn tại hay không tại route này — và đó là ĐIỂM
MẤU CHỐT, không phải một chỗ sót: thêm một bước tra cứu ở đây (dù chỉ để 404
sớm cho một round trip) là mở lại đúng lỗ rò rỉ sự tồn tại mà route POST/GET/
DELETE ở trên đã CHẤP NHẬN đánh đổi, còn route NÀY thì không được phép — xem
`services/api/tests/integration/test_lakehouse_schema_proxy.py` cho phép kiểm
canh đúng lỗi này.
"""

from __future__ import annotations

import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State
from starlette.responses import Response

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.models import ACTIVE, Item
from loom_core.config import Settings
from loom_core.internal_auth import QUERY_SHARED_SECRET_HEADER
from loom_core.item_definitions import ItemType
from loom_core.schemas import Principal, QuerySubmitRequest

router = APIRouter(tags=["query"])


async def _lakehouse_workspace_id(
    session: AsyncSession, lakehouse_id: uuid.UUID
) -> uuid.UUID | None:
    """`workspace_id` CHỨA `lakehouse_id`, hoặc `None` nếu nó không tồn tại,
    không phải type `lakehouse`, hoặc đã xoá mềm. KHÔNG kiểm quyền — xem
    docstring module."""
    stmt = select(Item.workspace_id).where(
        Item.id == lakehouse_id,
        Item.type == str(ItemType.lakehouse),
        Item.state == ACTIVE,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def query_request(
    app_state: State,
    settings: Settings,
    *,
    method: str,
    path: str,
    json_body: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Một round trip tới `loom-query`, có bí mật chia sẻ đính kèm.

    ĐÚNG MỘT chỗ trong `loom-api` dựng URL của `loom-query` và gắn
    `QUERY_SHARED_SECRET_HEADER`. Công khai (không còn nằm trong `_forward`) vì
    từ 3b có một người gọi THỨ HAI không phải một request của trình duyệt: nhịp
    lịch nộp SQL của một bước pipeline (`routers/internal_schedule.py`). Người
    gọi đó không có `Request` nào để đưa vào — nó chạy trong vòng lặp của tick,
    không trong một handler của người dùng — nên tham số là `app_state` +
    `settings` chứ không phải `Request`.

    Một bản chép ở chỗ gọi thứ hai sẽ là chỗ thứ hai có thể QUÊN header bí mật,
    và hậu quả không phải một lỗi lộ liễu: `loom-query` trả 401, bước SQL hỏng,
    và người đọc đi tìm nguyên nhân ở câu SQL.

    Trả về `httpx.Response` THÔ chứ không phải `Response` của Starlette: người
    gọi thứ hai cần ĐỌC thân phản hồi (lấy `query_id`, đọc `status`), không phải
    chuyển tiếp nó ra ngoài.
    """
    client: httpx.AsyncClient = app_state.query_http
    return await client.request(
        method,
        f"{settings.query_base_url}{path}",
        params=params,
        json=json_body,
        headers={QUERY_SHARED_SECRET_HEADER: settings.query_shared_secret},
    )


async def _forward(
    request: Request,
    *,
    method: str,
    path: str,
    json_body: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
) -> Response:
    settings: Settings = request.app.state.settings
    upstream = await query_request(
        request.app.state,
        settings,
        method=method,
        path=path,
        json_body=json_body,
        params=params,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@router.post("/query", status_code=status.HTTP_202_ACCEPTED)
async def create_query(
    body: QuerySubmitRequest,
    request: Request,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    workspace_id = await _lakehouse_workspace_id(session, body.lakehouse_id)
    if workspace_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lakehouse with this id")

    return await _forward(
        request,
        method="POST",
        path="/query",
        json_body={
            "lakehouse_id": str(body.lakehouse_id),
            # Giá trị THẬT vừa tra ở trên — KHÔNG PHẢI `body.workspace_id`.
            # Xem docstring module cho lý do tin giá trị client là một lỗ hổng.
            "workspace_id": str(workspace_id),
            "sql": body.sql,
            "principal": principal.model_dump(mode="json"),
        },
    )


@router.get("/query/{query_id}")
async def get_query(
    query_id: uuid.UUID,
    request: Request,
    principal: Principal = PrincipalDep,
) -> Response:
    return await _forward(request, method="GET", path=f"/query/{query_id}")


@router.delete("/query/{query_id}", status_code=status.HTTP_202_ACCEPTED)
async def cancel_query(
    query_id: uuid.UUID,
    request: Request,
    principal: Principal = PrincipalDep,
) -> Response:
    return await _forward(request, method="DELETE", path=f"/query/{query_id}")


@router.get("/lakehouses/{lakehouse_id}/schema")
async def lakehouse_schema(
    lakehouse_id: uuid.UUID,
    request: Request,
    principal: Principal = PrincipalDep,
    depth: Literal["tables", "columns"] = "tables",
) -> Response:
    """Chuyển tiếp sang `loom-query`'s `GET /api/v1/lakehouses/{lakehouse_id}/
    schema` — xem module docstring cho lý do route này KHÔNG tra
    `_lakehouse_workspace_id` như ba route trên.

    `principal` đi trong THÂN request (GET vẫn mang body — xem docstring
    `loom_query.schemas.SchemaRequest`), đúng cách `POST /query` chuyển tiếp
    principal của người dùng cuối, chỉ khác Ở CHỖ route này không có `sql`
    hay `workspace_id` nào để gửi kèm — không có SQL nào để phân giải tên
    bảng ba phần, nên không cần `workspace_id`."""
    return await _forward(
        request,
        method="GET",
        path=f"/lakehouses/{lakehouse_id}/schema",
        params={"depth": depth},
        json_body={"principal": principal.model_dump(mode="json")},
    )
