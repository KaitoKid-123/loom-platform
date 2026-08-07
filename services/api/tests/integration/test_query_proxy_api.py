"""`POST/GET/DELETE /api/v1/query*` chuyển tiếp sang `loom-query` — Task 10/11.

`loom-query` thật không chạy trong bộ test này — `api_world.app.state.
query_http` bị thay bằng một `httpx.MockTransport` (xem `conftest.py`), đúng
cách `test_oidc_client.py` giả Dex. Năm "chứng minh đỏ" bắt buộc của spec:

1. Chưa đăng nhập → 401, VÀ request không tới `loom-query`
   (`test_unauthenticated_post_never_reaches_query`,
   `test_unauthenticated_get_and_delete_never_reach_query`).
2/3. Bí mật chia sẻ SAI/thiếu → 401 — kiểm ở PHÍA `loom-query`
   (`services/loom-query/tests/test_query_shared_secret.py`), không lặp lại
   ở đây; bài test bên dưới chỉ khẳng định `loom-api` CÓ đính header đúng giá
   trị cấu hình vào mọi request nó gửi đi
   (`test_the_shared_secret_header_is_attached`).
4. Ingress không thêm path cho `loom-query` — canh ở
   `test_internal_route_boundary.py`
   (`test_ingress_still_only_serves_these_two_paths`), không lặp ở đây.
5. `workspace_id` client gửi bị BỎ QUA — `loom-api` luôn dùng workspace THẬT
   tra được từ `lakehouse_id` (`test_client_supplied_workspace_id_is_ignored`).
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


class _RecordingQueryBackend:
    """Một `loom-query` giả ghi lại MỌI request nó nhận, và trả về đáp ứng đã
    cấu hình sẵn cho từng method — đủ để kiểm chuyển tiếp mà không cần
    `loom-query` thật."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.post_response: httpx.Response = httpx.Response(
            202, json={"query_id": str(uuid.uuid4())}
        )
        self.get_response: httpx.Response = httpx.Response(200, json={"status": "running"})
        self.delete_response: httpx.Response = httpx.Response(202, json={"status": "cancelled"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return {
            "POST": self.post_response,
            "GET": self.get_response,
            "DELETE": self.delete_response,
        }[request.method]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def last_json(self) -> dict[str, Any]:
        return dict(json.loads(self.requests[-1].content))


def _body(lakehouse_id: uuid.UUID, sql: str = "SELECT 1", **extra: object) -> dict[str, object]:
    return {"lakehouse_id": str(lakehouse_id), "sql": sql, **extra}


async def test_unauthenticated_post_never_reaches_query(api_world: ApiWorld) -> None:
    """Gỡ `PrincipalDep` khỏi `create_query` để chứng minh đỏ: request sẽ đi
    xuyên qua `_lakehouse_workspace_id`/`_forward` và `backend.requests` sẽ
    không còn rỗng — 401 phải xảy ra TRƯỚC khi có bất kỳ I/O nào ra ngoài."""
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingQueryBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.post(
        "/api/v1/query", json=_body(lakehouse_id), cookies={"loom_session": "sai"}
    )

    assert r.status_code == 401
    assert backend.requests == []


async def test_unauthenticated_get_and_delete_never_reach_query(api_world: ApiWorld) -> None:
    backend = _RecordingQueryBackend()
    api_world.app.state.query_http = backend.client()
    query_id = uuid.uuid4()

    get_r = await api_world.client.get(f"/api/v1/query/{query_id}", cookies={"loom_session": "sai"})
    delete_r = await api_world.client.delete(
        f"/api/v1/query/{query_id}", cookies={"loom_session": "sai"}
    )

    assert get_r.status_code == 401
    assert delete_r.status_code == 401
    assert backend.requests == []


async def test_unknown_lakehouse_id_is_404_before_reaching_query(api_world: ApiWorld) -> None:
    backend = _RecordingQueryBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.post("/api/v1/query", json=_body(uuid.uuid4()))

    assert r.status_code == 404
    assert backend.requests == []


async def test_client_supplied_workspace_id_is_ignored(api_world: ApiWorld) -> None:
    """Chứng minh đỏ 5: `lakehouse_id` thuộc `ws_a`, client tự khai
    `workspace_id=ws_b` (workspace KHÁC) — `loom-api` phải dùng `ws_a` (giá
    trị THẬT, tra từ `lakehouse_id`) khi chuyển tiếp, không phải `ws_b`.

    Cho `loom-api` tin `body.workspace_id` (bỏ dòng tra cứu, dùng thẳng giá
    trị client gửi) → phép kiểm này phải ĐỎ: `forwarded["workspace_id"]` sẽ
    đọc ra `str(api_world.ws_b)` thay vì `str(api_world.ws_a)`.
    """
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingQueryBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.post(
        "/api/v1/query",
        json=_body(lakehouse_id, workspace_id=str(api_world.ws_b)),
    )

    assert r.status_code == 202, r.text
    assert len(backend.requests) == 1
    forwarded = backend.last_json()
    assert forwarded["workspace_id"] == str(api_world.ws_a)
    assert forwarded["lakehouse_id"] == str(lakehouse_id)


async def test_post_forwards_the_sessions_principal(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingQueryBackend()
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.post(
        "/api/v1/query", json=_body(lakehouse_id, sql="SELECT * FROM ns.t")
    )

    assert r.status_code == 202
    forwarded = backend.last_json()
    assert forwarded["principal"]["user_id"] == str(api_world.principal.user_id)
    assert forwarded["principal"]["subject"] == api_world.principal.subject
    assert forwarded["sql"] == "SELECT * FROM ns.t"


async def test_the_shared_secret_header_is_attached(api_world: ApiWorld) -> None:
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingQueryBackend()
    api_world.app.state.query_http = backend.client()

    await api_world.client.post("/api/v1/query", json=_body(lakehouse_id))

    settings = api_world.app.state.settings
    assert backend.requests[-1].headers[QUERY_SHARED_SECRET_HEADER] == settings.query_shared_secret


async def test_get_and_delete_pass_through_status_and_body(api_world: ApiWorld) -> None:
    backend = _RecordingQueryBackend()
    backend.get_response = httpx.Response(
        200, json={"status": "succeeded", "rows": [[1]], "columns": [{"name": "a", "type": "int"}]}
    )
    backend.delete_response = httpx.Response(404, json={"detail": "no query with this id"})
    api_world.app.state.query_http = backend.client()
    query_id = uuid.uuid4()

    get_r = await api_world.client.get(f"/api/v1/query/{query_id}")
    delete_r = await api_world.client.delete(f"/api/v1/query/{query_id}")

    assert get_r.status_code == 200
    assert get_r.json()["rows"] == [[1]]
    assert delete_r.status_code == 404
    assert delete_r.json() == {"detail": "no query with this id"}
    assert [str(r.url).endswith(f"/query/{query_id}") for r in backend.requests] == [True, True]


async def test_forbidden_response_from_query_passes_through_unchanged(api_world: ApiWorld) -> None:
    """`loom-query` trả 403 khi principal thiếu viewer trên một bảng — thân
    phản hồi PHẢI đi qua nguyên vẹn, không bị `install_error_handlers` của
    `loom-api` bọc lại thành một `ProblemDetail` khác hình dạng."""
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")
    backend = _RecordingQueryBackend()
    backend.post_response = httpx.Response(
        403, json={"detail": "you do not have permission to run this query"}
    )
    api_world.app.state.query_http = backend.client()

    r = await api_world.client.post("/api/v1/query", json=_body(lakehouse_id))

    assert r.status_code == 403
    assert r.json() == {"detail": "you do not have permission to run this query"}
