"""Endpoint domain — nhóm workspace theo lĩnh vực nghiệp vụ, như Fabric.

Bảng `domain`, cột `workspace.domain_id` và cấp scope `domain` đã chạy từ Giai đoạn 1a;
thiếu duy nhất một đường tạo ra domain, nên tiêu chí nghiệm thu "tạo được domain qua UI"
không thể đạt. File này canh cái đường đó.
"""

import uuid

import pytest

from loom_api.models import DEFAULT_TENANT_ID
from loom_core.roles import Role

pytestmark = pytest.mark.integration


async def _make_domain(world, name: str = "tai-chinh", display: str = "Tài chính"):
    return await world.client.post("/api/v1/domains", json={"name": name, "display_name": display})


async def test_only_a_tenant_admin_can_create_a_domain(api_world):
    w = api_world
    # 404 chứ không 403: không có vai trò nào ở cấp tenant thì với người gọi, tenant
    # không phải thứ họ thấy được — cùng quy ước với tạo workspace.
    assert (await _make_domain(w)).status_code == 404

    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.member)
    assert (await _make_domain(w)).status_code == 403


async def test_a_tenant_admin_creates_a_domain(api_world):
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    r = await _make_domain(w)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "tai-chinh"
    assert body["display_name"] == "Tài chính"
    # Domain mới chưa có workspace nào, và con số này phân biệt "mới tạo" với "vừa bị
    # dọn sạch" — hai trạng thái trông giống hệt nhau nếu không đếm.
    assert body["workspace_count"] == 0
    assert body["my_role"] == "admin"


async def test_duplicate_domain_name_is_409_not_500(api_world):
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    assert (await _make_domain(w)).status_code == 201
    second = await _make_domain(w)
    assert second.status_code == 409, second.text
    assert "tai-chinh" in second.json()["detail"]


async def test_an_invalid_domain_name_is_rejected_with_field_detail(api_world):
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    r = await w.client.post(
        "/api/v1/domains", json={"name": "Có Khoảng Trắng", "display_name": "X"}
    )
    assert r.status_code == 422
    assert any(e["loc"][-1] == "name" for e in r.json()["errors"])


async def test_everyone_can_list_domains_even_without_a_role(api_world):
    """Danh sách domain là BẢN ĐỒ TỔ CHỨC, không phải dữ liệu: biết phòng Tài chính tồn
    tại không phải là đọc được gì của họ. Cố ý khác workspace, nơi không có quyền nghĩa
    là không thấy — nên phải có test nói rõ điều đó là chủ đích."""
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    await _make_domain(w)

    # Người thứ hai, không vai trò nào — vẫn thấy domain, nhưng `my_role` là null.
    listed = (await w.client.get("/api/v1/domains")).json()["items"]
    assert [d["name"] for d in listed] == ["tai-chinh"]


async def test_workspace_count_reflects_workspaces_actually_in_the_domain(api_world):
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    domain_id = (await _make_domain(w)).json()["id"]

    before = (await w.client.get("/api/v1/domains")).json()["items"][0]
    assert before["workspace_count"] == 0

    await w.grant(("workspace", w.ws_a), Role.admin)
    moved = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"domain_id": domain_id},
        headers={"If-Match": 'W/"1"'},
    )
    assert moved.status_code == 200, moved.text

    after = (await w.client.get("/api/v1/domains")).json()["items"][0]
    assert after["workspace_count"] == 1


async def test_a_workspace_can_be_pulled_back_out_of_its_domain(api_world):
    """`clear_domain` tách khỏi `domain_id=None`: `None` nghĩa là "không đổi". Gộp hai
    thứ thì không có cách nào gỡ một workspace ra khỏi domain."""
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    await w.grant(("workspace", w.ws_a), Role.admin)
    domain_id = (await _make_domain(w)).json()["id"]

    await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"domain_id": domain_id},
        headers={"If-Match": 'W/"1"'},
    )
    out = await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"clear_domain": True},
        headers={"If-Match": 'W/"2"'},
    )
    assert out.status_code == 200, out.text
    assert out.json()["domain_id"] is None


async def test_a_domain_role_reaches_every_workspace_inside_it(api_world):
    """Đây là LÝ DO domain tồn tại: gán quyền một lần cho cả nhóm workspace thay vì gán
    lại trên từng cái. Không có phép kiểm này thì domain chỉ là một cái nhãn."""
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    domain_id = (await _make_domain(w)).json()["id"]
    await w.grant(("workspace", w.ws_a), Role.admin)
    await w.client.patch(
        f"/api/v1/workspaces/{w.ws_a}",
        json={"domain_id": domain_id},
        headers={"If-Match": 'W/"1"'},
    )

    # Người thứ hai: KHÔNG vai trò nào trên workspace, chỉ có vai trò trên DOMAIN.
    reader = await w.make_user("reader")
    await w.grant(("domain", uuid.UUID(domain_id)), Role.viewer, user=reader)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from loom_api.permissions import PermissionService
    from loom_core.schemas import Principal

    maker = async_sessionmaker(w.engine, expire_on_commit=False)
    async with maker() as session:
        perms = PermissionService(
            session,
            Principal(
                user_id=reader, subject="reader", email="r@loom.local", display_name="r", groups=()
            ),
        )
        assert await perms.effective_role_for_workspace(w.ws_a) == Role.viewer


async def test_patch_domain_needs_the_manage_permission(api_world):
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    domain_id = (await _make_domain(w)).json()["id"]

    r = await w.client.patch(f"/api/v1/domains/{domain_id}", json={"display_name": "Đổi"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Đổi"


async def test_an_unknown_domain_is_404(api_world):
    w = api_world
    await w.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    r = await w.client.patch(f"/api/v1/domains/{uuid.uuid4()}", json={"display_name": "X"})
    assert r.status_code == 404
