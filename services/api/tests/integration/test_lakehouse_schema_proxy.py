"""`GET /api/v1/lakehouses/{lakehouse_id}/schema` chuyển tiếp sang `loom-query`
— Task 2, Giai đoạn 2c. Cùng khuôn `test_query_proxy_api.py` (Task 10/11):
`loom-query` thật không chạy trong bộ test này, `api_world.app.state.
query_http` bị thay bằng một `httpx.MockTransport`.

Bốn "chứng minh đỏ"/khẳng định bắt buộc của spec, khác với `POST /query`:

1. Chưa đăng nhập -> 401, VÀ request không tới `loom-query`
   (`test_unauthenticated_get_never_reaches_query`).
2. `lakehouse_id` KHÔNG TỒN TẠI chút nào (không một hàng `Item` nào khớp) vẫn
   phải ĐI THẲNG tới `loom-query` — KHÔNG một 404 sớm nào từ `loom-api` như
   `POST /query` làm với `_lakehouse_workspace_id`
   (`test_a_nonexistent_lakehouse_id_still_reaches_query`). Đây là chứng minh
   đỏ trung tâm: thêm một bước tra `_lakehouse_workspace_id`-kiểu vào route
   này (bắt chước ba route kia) sẽ làm bài này ĐỎ — response sẽ là 404 từ
   `loom-api` thay vì phản hồi mà "loom-query" (backend giả) đã cấu hình, và
   `backend.requests` sẽ rỗng thay vì có đúng một phần tử.
3. Một `lakehouse_id` TỒN TẠI THẬT (có hàng `Item`) nhưng không có bất kỳ vai
   trò nào cũng đi qua ĐÚNG một đường code — response giống hệt bài 2, byte vì
   byte (`test_an_existing_lakehouse_without_a_role_gets_the_identical_response`).
   Hai bài 2/3 cùng nhau là bằng chứng "tồn tại" và "không tồn tại" không thể
   phân biệt được ở tầng `loom-api` — cổng quyền THẬT (403 hay không) là việc
   của `loom-query`, không phải của route proxy này.
4. `depth` (`?depth=`) và `principal` được chuyển tiếp đúng, kèm bí mật chia
   sẻ — cùng khuôn các bài chuyển tiếp khác của `test_query_proxy_api.py`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, Item
from loom_core.internal_auth import QUERY_SHARED_SECRET_HEADER
from loom_core.item_definitions import ItemType

from .conftest import ApiWorld

pytestmark = pytest.mark.integration


async def _insert_lakehouse(world: ApiWorld, workspace_id: uuid.UUID, name: str) -> uuid.UUID:
    item_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type=str(ItemType.lakehouse),
                name=name,
                display_name=name,
                definition={"schema_version": 1},
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


class _RecordingSchemaBackend:
    """Một `loom-query` giả ghi lại MỌI request nó nhận — cùng vai trò
    `_RecordingQueryBackend` của `test_query_proxy_api.py`, tách riêng vì file
    đó chỉ cấu hình phản hồi theo HTTP method (một GET duy nhất, `/query/
    {id}`), còn route schema cũng là GET — dùng chung sẽ mơ hồ route nào đang
    được kiểm."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response: httpx.Response = httpx.Response(200, json={"namespaces": []})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def last_json(self) -> dict[str, Any]:
        return dict(json.loads(self.requests[-1].content))


async def test_unauthenticated_get_never_reaches_query(api_world: ApiWorld) -> None:
    """Chứng minh đỏ 3 của spec Task 2: gỡ `PrincipalDep` khỏi
    `lakehouse_schema` để thấy bài này ĐỎ — request sẽ đi xuyên qua `_forward`
    và `backend.requests` sẽ không còn rỗng."""
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.get(
        f"/api/v1/lakehouses/{lakehouse_id}/schema", cookies={"loom_session": "sai"}
    )

    assert r.status_code == 401
    assert backend.requests == []


async def test_a_nonexistent_lakehouse_id_still_reaches_query(api_world: ApiWorld) -> None:
    """Chứng minh đỏ trung tâm (yêu cầu 2 của spec Task 2): xem docstring
    module. `lakehouse_id` ở đây KHÔNG hề có hàng `Item` nào — nếu route thêm
    một bước tra `_lakehouse_workspace_id`-kiểu, `loom-api` sẽ tự trả 404 và
    KHÔNG BAO GIỜ hỏi backend — `backend.requests` sẽ rỗng và status sẽ là 404
    thay vì 403 đã cấu hình dưới đây."""
    backend = _RecordingSchemaBackend()
    backend.response = httpx.Response(
        403, json={"detail": "you do not have permission to read this lakehouse's schema"}
    )
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.get(f"/api/v1/lakehouses/{uuid.uuid4()}/schema")

    assert r.status_code == 403
    assert r.json() == {"detail": "you do not have permission to read this lakehouse's schema"}
    assert len(backend.requests) == 1


async def test_an_existing_lakehouse_without_a_role_gets_the_identical_response(
    api_world: ApiWorld,
) -> None:
    """Cùng response, byte vì byte, với `test_a_nonexistent_lakehouse_id_
    still_reaches_query` — `lakehouse_id` ở đây TỒN TẠI THẬT (một hàng `Item`
    có thật), nhưng principal của `api_world` không có vai trò gì trên nó.
    `loom-api` không phân biệt hai kịch bản này — cả hai đi qua ĐÚNG MỘT đường
    `_forward`, không rẽ nhánh nào theo "id có tồn tại hay không"."""
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_b, "khong-co-quyen")
    backend = _RecordingSchemaBackend()
    backend.response = httpx.Response(
        403, json={"detail": "you do not have permission to read this lakehouse's schema"}
    )
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema")

    assert r.status_code == 403
    assert r.json() == {"detail": "you do not have permission to read this lakehouse's schema"}
    assert len(backend.requests) == 1


async def test_the_shared_secret_header_is_attached(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    api_world.app.state.query_http = backend.client()

    await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema")

    settings = api_world.app.state.settings
    assert backend.requests[-1].headers[QUERY_SHARED_SECRET_HEADER] == settings.query_shared_secret


async def test_the_sessions_principal_is_forwarded_in_the_body(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    api_world.app.state.query_http = backend.client()

    await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema")

    forwarded = backend.last_json()
    assert forwarded["principal"]["user_id"] == str(api_world.principal.user_id)
    assert forwarded["principal"]["subject"] == api_world.principal.subject
    assert "sql" not in forwarded
    assert "workspace_id" not in forwarded


async def test_default_depth_forwarded_as_tables(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    api_world.app.state.query_http = backend.client()

    await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema")

    assert backend.requests[-1].url.params["depth"] == "tables"


async def test_explicit_depth_columns_is_forwarded(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=columns")

    assert r.status_code == 200
    assert backend.requests[-1].url.params["depth"] == "columns"


async def test_an_invalid_depth_value_is_422_before_reaching_query(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=bogus")

    assert r.status_code == 422
    assert backend.requests == []


async def test_successful_response_body_passes_through_unchanged(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingSchemaBackend()
    backend.response = httpx.Response(
        200,
        json={
            "namespaces": [
                {
                    "name": "sales",
                    "tables": [{"name": "orders", "columns": [{"name": "id", "type": "int64"}]}],
                }
            ]
        },
    )
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.get(f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=columns")

    assert r.status_code == 200
    assert r.json() == backend.response.json()
