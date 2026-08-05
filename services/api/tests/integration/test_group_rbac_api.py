"""Vai trò gán cho một NHÓM, kiểm qua đúng đường HTTP.

Cho tới Task 25 chuỗi này chưa từng chạy thật một lần nào. `staticPasswords` của
Dex không phát claim `groups`, và Loom cũng không yêu cầu scope `groups` — nên mọi
test về nhóm trước đây đều dựng `Principal` bằng tay ở tầng store, và cả tầng router
lẫn cả ba biểu thức SQL chưa bao giờ thấy một phiên có nhóm.

`make smoke` phép 9 canh nửa đầu của chuỗi (Dex → id_token → session → /me) và cần
một cụm đang sống. File này canh nửa sau (phiên có nhóm → `role_assignment
.principal_group` → biểu thức lọc) và chạy trong CI. Thiếu nửa nào thì tính năng
vẫn chết, nên phải có cả hai.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, Item
from loom_core.roles import Role

GROUP = "authors"

# Mọi test trong file này chạy với một phiên MANG nhóm. Đặt ở cấp module chứ không
# lặp trên từng test: một test lỡ thiếu marker sẽ chạy với principal không nhóm và
# xanh vì không thấy gì cả — chứ không vì phân quyền theo nhóm hoạt động.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("api_world", [(GROUP,)], indirect=True),
]


async def _insert_item(world, workspace_id: uuid.UUID, name: str) -> uuid.UUID:
    item_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type="sql_script",
                name=name,
                display_name=name,
                definition={"schema_version": 1, "sql": ""},
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


async def test_the_session_actually_carries_the_group(api_world):
    """Tiền đề của mọi test dưới đây. Nếu phiên không mang nhóm thì chúng xanh vì
    không có gì thấy được, chứ không vì phân quyền theo nhóm hoạt động."""
    r = await api_world.client.get("/api/v1/me")
    assert r.json()["groups"] == [GROUP]


async def test_a_role_granted_to_a_group_makes_the_workspace_visible(api_world):
    """Cá nhân người này KHÔNG có vai trò nào ở đâu cả — mọi thứ họ thấy đều đến từ
    nhóm. Đó là điều làm phép kiểm này nhìn thấy được thứ nó đặt tên."""
    w = api_world
    assert (await w.client.get("/api/v1/workspaces")).json()["items"] == []

    await w.grant(("workspace", w.ws_a), Role.viewer, group=GROUP)

    body = (await w.client.get("/api/v1/workspaces")).json()
    assert {i["id"] for i in body["items"]} == {str(w.ws_a)}
    # `my_role` phải là vai trò của NHÓM. Trả `None` hay rỗng ở đây thì giao diện
    # ẩn hết mọi nút và người dùng thấy một workspace không làm gì được.
    assert body["items"][0]["my_role"] == "viewer"


async def test_a_group_role_reaches_items_inside_the_workspace(api_world):
    """Biểu thức thứ hai — `visible_items_select`. Nó có nhánh nhóm riêng, nên nó
    hỏng riêng được so với `visible_workspaces_select`."""
    w = api_world
    item_id = await _insert_item(w, w.ws_a, "cua-nhom")
    assert (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/items")).status_code == 404

    await w.grant(("workspace", w.ws_a), Role.viewer, group=GROUP)

    listed = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/items")).json()["items"]
    assert {i["id"] for i in listed} == {str(item_id)}
    assert (await w.client.get(f"/api/v1/items/{item_id}")).status_code == 200


async def test_a_group_role_reaches_search(api_world):
    """Biểu thức thứ ba. `search` không nhận `workspace_id` trong đường dẫn nên nó
    là chỗ dễ nhất để một nhánh quyền bị viết thiếu."""
    w = api_world
    item_id = await _insert_item(w, w.ws_a, "tim-theo-nhom")
    assert (await w.client.get("/api/v1/search", params={"q": "tim-theo"})).json()["items"] == []

    await w.grant(("workspace", w.ws_a), Role.viewer, group=GROUP)

    found = (await w.client.get("/api/v1/search", params={"q": "tim-theo"})).json()["items"]
    assert {i["id"] for i in found} == {str(item_id)}


async def test_a_role_granted_to_another_group_grants_nothing(api_world):
    """Vế đối. Không có nó, một bản cài đặt bỏ hẳn điều kiện so tên nhóm — cho ai
    có bất kỳ nhóm nào cũng thấy mọi grant nhóm — vẫn xanh hết ở trên."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.viewer, group="ke-toan")
    assert (await w.client.get("/api/v1/workspaces")).json()["items"] == []


async def test_the_group_role_is_capped_by_what_the_group_was_given(api_world):
    """Vai trò đến từ nhóm phải chịu ĐÚNG những giới hạn của vai trò đó. Một nhánh
    nhóm bỏ qua `_roles_allowing` sẽ cho đọc mà cũng cho ghi."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.viewer, group=GROUP)

    r = await w.client.post(
        f"/api/v1/workspaces/{w.ws_a}/items",
        json={
            "type": "sql_script",
            "name": "khong-duoc-ghi",
            "display_name": "Không được ghi",
            "definition": {"schema_version": 1, "sql": "SELECT 1"},
        },
    )
    assert r.status_code == 403


async def test_a_group_role_does_not_leak_across_workspaces(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.viewer, group=GROUP)
    assert (await w.client.get(f"/api/v1/workspaces/{w.ws_b}")).status_code == 404
