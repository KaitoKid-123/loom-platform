"""Endpoint đọc audit, qua đúng đường HTTP.

Điểm chính của endpoint này là `request_id`: nó là sợi dây duy nhất nối một thay đổi
trong database với dòng log của đúng request đã gây ra nó. Test ở tầng store kiểm
được cổng quyền; chỉ đi qua HTTP mới thấy được `request_id` mà middleware gán cho
request có thật sự đi tới hàng audit hay không.
"""

import uuid

import pytest

from loom_core.roles import Role

pytestmark = pytest.mark.integration

_SQL = {"schema_version": 1, "sql": "SELECT 1"}


async def _make_item(world, name: str = "co-audit") -> tuple[uuid.UUID, str]:
    r = await world.client.post(
        f"/api/v1/workspaces/{world.ws_a}/items",
        json={"type": "sql_script", "name": name, "display_name": name, "definition": _SQL},
    )
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"]), r.headers["x-request-id"]


async def test_the_audit_row_carries_the_request_id_of_the_request_that_caused_it(api_world):
    """Không có sợi dây này thì người vận hành có bảng audit, có log, và không có
    cách nào ghép hai thứ lại."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    item_id, request_id = await _make_item(w)

    rows = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit")).json()["items"]
    mine = [r for r in rows if r["resource_id"] == str(item_id)]
    assert [r["action"] for r in mine] == ["item.create"]
    assert mine[0]["request_id"] == request_id, "request_id không khớp header của request"


async def test_one_audit_row_per_change(api_world):
    """Một dòng cho MỖI thay đổi. Thiếu dòng thì lịch sử có lỗ; thừa dòng thì người
    đọc tưởng có hai lần sửa."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    item_id, _ = await _make_item(w)

    r = await w.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "Đã đổi"},
        headers={"If-Match": 'W/"1"'},
    )
    assert r.status_code == 200, r.text
    assert (await w.client.delete(f"/api/v1/items/{item_id}")).status_code == 204

    rows = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit")).json()["items"]
    mine = [r for r in rows if r["resource_id"] == str(item_id)]
    # Mới nhất trước — thứ tự của `AuditReader`.
    assert [r["action"] for r in mine] == ["item.delete", "item.update", "item.create"]
    # `request_id` KHÁC nhau giữa ba request. Bằng nhau nghĩa là middleware tái dùng
    # một giá trị, và lúc đó sợi dây tới log chỉ vào ba chỗ cùng lúc.
    assert len({r["request_id"] for r in mine}) == 3


async def test_a_noop_patch_writes_no_audit_row(api_world):
    """`PATCH` không đổi gì không bump version, nên nó cũng không được sinh dòng
    audit — nếu có thì lịch sử đầy những "đã sửa" mà không sửa gì."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    item_id, _ = await _make_item(w)

    r = await w.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "co-audit"},
        headers={"If-Match": 'W/"1"'},
    )
    assert r.status_code == 200

    rows = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit")).json()["items"]
    mine = [r for r in rows if r["resource_id"] == str(item_id)]
    assert [r["action"] for r in mine] == ["item.create"]


async def test_a_contributor_cannot_read_the_audit(api_world):
    """403 chứ không 404: contributor ĐỌC được workspace. Spec mục 6 — sửa được item
    không kéo theo quyền biết ai khác đã sửa gì."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.contributor)
    r = await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit")
    assert r.status_code == 403


async def test_a_stranger_gets_404_on_the_audit(api_world):
    w = api_world
    r = await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit")
    assert r.status_code == 404


async def test_the_audit_of_another_workspace_never_leaks(api_world):
    """`member` trên `ws_a` không được thấy audit của `ws_b`, kể cả khi hai workspace
    cùng một tenant và cùng một người thao tác."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    await w.grant(("workspace", w.ws_b), Role.admin)
    _, _ = await _make_item(w, "cua-a")
    b = await w.client.post(
        f"/api/v1/workspaces/{w.ws_b}/items",
        json={"type": "sql_script", "name": "cua-b", "display_name": "cua-b", "definition": _SQL},
    )
    assert b.status_code == 201

    rows = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit")).json()["items"]
    ids = {r["resource_id"] for r in rows}
    assert str(uuid.UUID(b.json()["id"])) not in ids


async def test_audit_filters_by_resource_id(api_world):
    """Spec mục 6 đòi lọc theo `resource_id` — không có nó thì không xem được lịch sử
    của MỘT item, phải đọc cả workspace rồi tự lọc bằng mắt."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    first, _ = await _make_item(w, "item-mot")
    second, _ = await _make_item(w, "item-hai")

    rows = (
        await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit", params={"resource_id": str(first)})
    ).json()["items"]
    assert {r["resource_id"] for r in rows} == {str(first)}
    assert str(second) not in {r["resource_id"] for r in rows}


async def test_resource_id_of_another_workspace_returns_nothing(api_world):
    """Lọc THÊM, không thay: `workspace_id` vẫn ở trong đường dẫn, nên một `resource_id`
    thuộc workspace khác cho ra rỗng chứ không cho đọc chéo."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    await w.grant(("workspace", w.ws_b), Role.admin)
    await _make_item(w, "cua-a")
    other = await w.client.post(
        f"/api/v1/workspaces/{w.ws_b}/items",
        json={"type": "sql_script", "name": "cua-b", "display_name": "cua-b", "definition": _SQL},
    )
    other_id = other.json()["id"]

    rows = (
        await w.client.get(f"/api/v1/workspaces/{w.ws_a}/audit", params={"resource_id": other_id})
    ).json()["items"]
    assert rows == []
