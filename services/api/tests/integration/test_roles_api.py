"""Endpoint gán/thu vai trò, qua đúng đường HTTP.

Hai lớp phòng vệ khác nhau và cả hai đều cần:

- `grantable_roles` trong phản hồi CHỈ để giao diện không hiện tuỳ chọn mà server
  sẽ từ chối. Gỡ nó đi thì UI xấu.
- `RoleStore.grant` là thứ THẬT SỰ chặn. Gỡ nó đi thì bất kỳ ai gọi API trực tiếp
  bằng curl đều tự nâng mình lên admin được.

Mỗi lớp có test riêng ở dưới, và đó là cố ý: một test canh cả hai sẽ vẫn xanh khi
lớp quan trọng hơn biến mất.
"""

import uuid

import pytest

from loom_core.roles import Role

pytestmark = pytest.mark.integration


async def test_member_does_not_see_admin_in_grantable_roles(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.member)

    r = await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")
    assert r.status_code == 200, r.text
    # Đẳng thức trên cả tập, không chỉ `"admin" not in`: một bản cài đặt trả về
    # danh sách rỗng cũng qua được phép kiểm phủ định đó, và một ô chọn rỗng là
    # một lỗi khác chứ không phải thành công.
    assert r.json()["grantable_roles"] == ["viewer", "contributor"]


async def test_admin_sees_every_role_including_admin(api_world):
    """Vế đối của test trên. Không có nó, một bản cài đặt luôn trả `[]` vẫn xanh."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)

    r = await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")
    assert r.json()["grantable_roles"] == ["viewer", "contributor", "member", "admin"]


async def test_grantable_roles_is_sorted_by_rank_not_alphabetically(api_world):
    """Giao diện hiện đúng thứ tự này trong ô chọn. Sắp theo bảng chữ cái cho ra
    "admin, contributor, member, viewer" — đọc như một danh sách ngẫu nhiên."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    roles = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["grantable_roles"]
    assert roles == sorted(roles, key=lambda name: Role[name])
    assert roles != sorted(roles)


async def test_member_granting_admin_is_403(api_world):
    """LỚP THẬT. `grantable_roles` chỉ là gợi ý cho UI; đây là chỗ chặn."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.member)
    target = await w.make_user("bob")

    r = await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "admin", "user_id": str(target)},
    )
    assert r.status_code == 403, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_member_cannot_promote_itself(api_world):
    """Cách khai thác trực tiếp nhất nếu quy tắc 1 mất: một lệnh, không cần ai duyệt."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.member)

    r = await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "admin", "user_id": str(w.user_id)},
    )
    assert r.status_code == 403
    # Và nó THẬT SỰ không đổi được gì — 403 mà hàng vẫn được ghi là tệ hơn 200.
    after = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()
    mine = [i for i in after["items"] if i["user_id"] == str(w.user_id)]
    assert [i["role"] for i in mine] == ["member"]


async def test_member_cannot_revoke_an_admin(api_world):
    """Quy tắc 3. Không có nó, member không GÁN được admin nhưng lại THU được của
    một admin đang có — thứ bạn không được phép cho, bạn lại được phép lấy đi."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.member)
    boss = await w.make_user("boss")
    await w.grant(("workspace", w.ws_a), Role.admin, user=boss)

    r = await w.client.delete(f"/api/v1/workspaces/{w.ws_a}/roles?user_id={boss}")
    assert r.status_code == 403, r.text


async def test_admin_grant_then_revoke_round_trips(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    target = await w.make_user("bob")

    put = await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "contributor", "user_id": str(target)},
    )
    assert put.status_code == 204, put.text

    listed = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["items"]
    assert {(i["user_id"], i["role"]) for i in listed} == {
        (str(w.user_id), "admin"),
        (str(target), "contributor"),
    }

    delete = await w.client.delete(f"/api/v1/workspaces/{w.ws_a}/roles?user_id={target}")
    assert delete.status_code == 204, delete.text

    after = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["items"]
    assert {i["user_id"] for i in after} == {str(w.user_id)}


async def test_regranting_changes_the_role_instead_of_conflicting(api_world):
    """Cùng principal + cùng scope là ĐỔI vai trò, không phải lỗi trùng. Trả 409 ở
    đây buộc giao diện phải thu rồi gán lại — hai bước, và giữa hai bước đó người
    kia mất quyền."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    target = await w.make_user("bob")

    for role in ("viewer", "member"):
        r = await w.client.put(
            f"/api/v1/workspaces/{w.ws_a}/roles",
            json={"role": role, "user_id": str(target)},
        )
        assert r.status_code == 204, f"{role}: {r.text}"

    listed = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["items"]
    theirs = [i for i in listed if i["user_id"] == str(target)]
    assert [i["role"] for i in theirs] == ["member"], "phải là MỘT hàng, đã đổi vai trò"


async def test_revoking_the_last_admin_is_409_with_a_usable_message(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)

    r = await w.client.delete(f"/api/v1/workspaces/{w.ws_a}/roles?user_id={w.user_id}")
    assert r.status_code == 409, r.text
    # Thông báo phải nói người dùng làm gì TIẾP, không chỉ nói "không được".
    assert "grant another admin" in r.json()["detail"]


async def test_grant_requires_exactly_one_principal(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    target = await w.make_user("bob")

    both = await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "viewer", "user_id": str(target), "group": "data-eng"},
    )
    assert both.status_code == 422, both.text
    # Có `errors[]` để frontend gắn lỗi vào đúng ô — đó là lý do quy tắc này nằm
    # ở schema chứ không ở router.
    assert both.json()["errors"]

    neither = await w.client.put(f"/api/v1/workspaces/{w.ws_a}/roles", json={"role": "viewer"})
    assert neither.status_code == 422, neither.text


async def test_revoke_requires_exactly_one_principal(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)

    r = await w.client.delete(f"/api/v1/workspaces/{w.ws_a}/roles")
    assert r.status_code == 422, r.text


async def test_granting_an_unknown_user_is_422_not_500(api_world):
    """`user_id` là dữ liệu client gửi lên và một danh sách người dùng đã cũ trên
    giao diện là cách bình thường nhất để nó sai. Khoá ngoại vỡ mà không ai bắt thì
    client nhận 500 với một thân phản hồi cố tình không nói gì."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)

    r = await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "viewer", "user_id": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_a_group_can_be_granted_a_role(api_world):
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)

    r = await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "viewer", "group": "data-eng"},
    )
    assert r.status_code == 204, r.text

    listed = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["items"]
    row = next(i for i in listed if i["group"] == "data-eng")
    assert row["principal_type"] == "group"
    assert row["user_id"] is None


async def test_revoking_a_user_does_not_revoke_the_group(api_world):
    """`principal_matches` gộp cả nhóm của người gọi khi TRẢ LỜI quyền; nếu lệnh
    thu dùng lại biểu thức đó thì thu một người thành thu cả nhóm."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles", json={"role": "viewer", "group": "data-eng"}
    )
    target = await w.make_user("bob")
    await w.client.put(
        f"/api/v1/workspaces/{w.ws_a}/roles",
        json={"role": "viewer", "user_id": str(target)},
    )

    r = await w.client.delete(f"/api/v1/workspaces/{w.ws_a}/roles?user_id={target}")
    assert r.status_code == 204, r.text

    listed = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["items"]
    assert "data-eng" in {i["group"] for i in listed}
    assert str(target) not in {i["user_id"] for i in listed}


async def test_the_role_list_is_ordered_by_when_the_role_was_granted(api_world):
    """Không có `ORDER BY` thì Postgres tự do trả theo thứ tự nào cũng được, và bảng
    quyền đổi chỗ giữa hai lần tải — người dùng đọc đó là "có ai vừa sửa gì"."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    first = await w.make_user("bob")
    second = await w.make_user("carol")
    for uid in (first, second):
        r = await w.client.put(
            f"/api/v1/workspaces/{w.ws_a}/roles",
            json={"role": "viewer", "user_id": str(uid)},
        )
        assert r.status_code == 204, r.text

    listed = (await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")).json()["items"]
    assert [i["user_id"] for i in listed] == [str(w.user_id), str(first), str(second)]


async def test_a_contributor_cannot_read_the_role_list(api_world):
    """`role.read` là từ member trở lên. 403 chứ không 404: contributor ĐỌC được
    workspace, nên nói không tìm thấy là gây hiểu nhầm là nó vừa biến mất."""
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.contributor)
    r = await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")
    assert r.status_code == 403


async def test_a_stranger_gets_404_on_the_role_list(api_world):
    w = api_world
    r = await w.client.get(f"/api/v1/workspaces/{w.ws_a}/roles")
    assert r.status_code == 404


@pytest.mark.parametrize("scope", ["domains", "tenants", "khong-co-thuc"])
async def test_no_route_exists_for_other_scopes(api_world, scope):
    """Không có route nào cho domain/tenant, nên chúng 404 vì KHÔNG TỒN TẠI chứ
    không vì một phép kiểm nhớ chặn. Đây cũng là phép kiểm rằng không có route bắt
    tất cả `/{scope_type}/{id}/roles` nào lỡ khớp những đường này."""
    w = api_world
    r = await w.client.get(f"/api/v1/{scope}/{uuid.uuid4()}/roles")
    assert r.status_code == 404
