"""Endpoint workspace, kiểm qua đúng đường HTTP.

Một cổng quyền đúng ở tầng store vẫn có thể bị bỏ qua ở tầng router, và chỉ test
qua HTTP mới thấy được điều đó — cùng với mã trạng thái, header, và hình dạng lỗi
mà client thật sự nhận.
"""

import uuid

import pytest

from loom_api.models import DEFAULT_TENANT_ID
from loom_core.roles import Role

pytestmark = pytest.mark.integration


async def test_list_shows_only_visible_workspaces(api_world):
    w = api_world
    r = await w.client.get("/api/v1/workspaces")
    assert r.status_code == 200
    assert r.json()["items"] == []

    await w.grant(("workspace", w.ws_a), Role.viewer)
    r = await w.client.get("/api/v1/workspaces")
    assert {item["id"] for item in r.json()["items"]} == {str(w.ws_a)}


async def test_my_role_is_the_callers_role_not_the_highest_present(api_world):
    """Frontend dùng `my_role` để ẩn nút server sẽ từ chối. Trả vai trò cao nhất
    có trong workspace thay vì của người gọi là chỉ cho họ nút họ không bấm được —
    và tệ hơn, tiết lộ rằng có người khác quyền cao hơn."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.viewer)

    r = await w.client.get("/api/v1/workspaces")
    assert r.json()["items"][0]["my_role"] == "viewer"


async def test_create_requires_tenant_admin(api_world):
    w = api_world
    r = await w.client.post("/api/v1/workspaces", json={"name": "moi", "display_name": "Mới"})
    # 404 vì không có vai trò nào ở cấp tenant — với người gọi thì tenant không
    # phải thứ họ thấy được.
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_tenant_admin_can_create_and_prefix_follows_the_id(api_world):
    """`storage_prefix` theo ID chứ không theo tên — đổi tên workspace không được
    làm đổi vị trí dữ liệu trên object storage ở Giai đoạn 2."""
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from loom_api.models import Workspace

    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)

    r = await w.client.post(
        "/api/v1/workspaces", json={"name": "moi-tao", "display_name": "Mới tạo"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["my_role"] == "admin"

    created = uuid.UUID(body["id"])
    maker = async_sessionmaker(w.engine)
    async with maker() as session:
        prefix = (
            await session.execute(select(Workspace.storage_prefix).where(Workspace.id == created))
        ).scalar_one()
        await session.execute(delete(Workspace).where(Workspace.id == created))
        await session.commit()
    assert prefix == f"workspaces/{created}"
    assert "moi-tao" not in prefix


async def test_create_rejects_an_invalid_name_with_field_detail(api_world):
    """`errors[]` từ Task 1 là thứ cho frontend gắn lỗi vào đúng ô input."""
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)

    r = await w.client.post(
        "/api/v1/workspaces", json={"name": "Có Khoảng Trắng", "display_name": "X"}
    )
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    assert any(e["loc"][-1] == "name" for e in r.json()["errors"])


async def test_get_unknown_workspace_is_404_not_500(api_world):
    w = api_world
    r = await w.client.get(f"/api/v1/workspaces/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_viewer_cannot_delete_a_workspace(api_world):
    """403 chứ không 404: viewer ĐỌC được workspace, nên nói không tìm thấy là gây
    hiểu nhầm là nó vừa biến mất."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.viewer)
    r = await w.client.delete(f"/api/v1/workspaces/{w.ws_a}")
    assert r.status_code == 403


async def test_admin_delete_soft_deletes_and_then_it_is_gone(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_b), Role.admin)

    r = await w.client.delete(f"/api/v1/workspaces/{w.ws_b}")
    assert r.status_code == 204
    assert (await w.client.get(f"/api/v1/workspaces/{w.ws_b}")).status_code == 404
    # Và nó rời khỏi danh sách, không chỉ khỏi endpoint lẻ.
    assert (await w.client.get("/api/v1/workspaces")).json()["items"] == []


async def test_an_unauthenticated_caller_gets_401_problem_json(api_world):
    w = api_world
    r = await w.client.get("/api/v1/workspaces", cookies={"loom_session": "sai"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_the_list_reports_the_tenant_role_so_the_ui_can_hide_create(api_world):
    """`my_role` chỉ nói vai trò trong MỘT workspace. Không có `tenant_role`, giao diện
    không có cách nào biết người dùng tạo được workspace hay không — và đó chính là lý
    do trang danh sách workspace từng không có nút tạo nào.

    Ở trên endpoint DANH SÁCH chứ không `/me`: `/me` cố ý không chạm database và được
    gọi mỗi lần tải trang."""
    w = api_world
    assert (await w.client.get("/api/v1/workspaces")).json()["tenant_role"] is None

    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    assert (await w.client.get("/api/v1/workspaces")).json()["tenant_role"] == "admin"


async def test_tenant_role_is_the_callers_role_not_anyone_elses(api_world):
    """Vế đối. Một bản cài đặt trả vai trò cao nhất có trong tenant sẽ hiện nút tạo cho
    mọi người, và tệ hơn, tiết lộ rằng có người khác quyền cao hơn."""
    w = api_world
    other = await w.make_user("boss")
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin, user=other)
    assert (await w.client.get("/api/v1/workspaces")).json()["tenant_role"] is None


async def test_me_still_does_not_touch_the_database(api_world):
    """`/me` gọi mỗi lần tải trang. Thêm một truy vấn vào đó là thêm một round trip cho
    mọi người dùng, mãi mãi — và câu hỏi đó endpoint danh sách trả lời được miễn phí."""
    body = (await w_me(api_world)).json()
    assert "tenant_role" not in body


async def w_me(world):
    return await world.client.get("/api/v1/me")


async def test_patch_workspace_needs_if_match_and_bumps_the_version(api_world):
    """Spec mục 6 đòi `If-Match` cho PATCH workspace. Không có nó, hai admin đổi tên
    cùng lúc thì người sau ghi đè người trước và không ai biết."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)

    got = await w.client.get(f"/api/v1/workspaces/{w.ws_a}")
    assert got.headers["etag"] == 'W/"1"'

    missing = await w.client.patch(f"/api/v1/workspaces/{w.ws_a}", json={"display_name": "Đổi"})
    assert missing.status_code == 428

    ok = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"display_name": "Tên mới"},
        headers={"If-Match": 'W/"1"'},
    )
    assert ok.status_code == 200, ok.text
    assert ok.headers["etag"] == 'W/"2"'
    assert ok.json()["display_name"] == "Tên mới"

    stale = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"display_name": "Muộn"},
        headers={"If-Match": 'W/"1"'},
    )
    assert stale.status_code == 412
    assert "2" in stale.json()["detail"]


async def test_a_noop_patch_keeps_the_workspace_version(api_world):
    """Bump vô cớ làm ETag của mọi client khác hết hạn, và chạm `updated_at` thì
    workspace nhảy lên đầu danh sách trong khi không ai đổi gì."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    current = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}")).json()

    r = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"display_name": current["display_name"]},
        headers={"If-Match": 'W/"1"'},
    )
    assert r.status_code == 200
    assert r.headers["etag"] == 'W/"1"'


async def test_viewer_cannot_patch_a_workspace(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.viewer)
    r = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"display_name": "X"},
        headers={"If-Match": 'W/"1"'},
    )
    assert r.status_code == 403


async def test_patch_to_an_unknown_domain_is_422_not_500(api_world):
    """`domain_id` là dữ liệu client gửi lên; khoá ngoại vỡ mà không ai bắt thì client
    nhận 500 với một thân phản hồi cố ý không nói gì."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    r = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"domain_id": str(uuid.uuid4())},
        headers={"If-Match": 'W/"1"'},
    )
    assert r.status_code == 422, r.text
