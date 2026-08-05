import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from loom_api.models import DEFAULT_TENANT_ID, RoleAssignment
from loom_api.permissions import Forbidden, NotVisible
from loom_api.role_store import LastAdminError, RoleStore
from loom_core.roles import Role

pytestmark = pytest.mark.integration


async def _roles_at(session, scope_type, scope_id):
    """Đọc THẲNG từ bảng, không qua `list_roles`.

    `list_roles` là code đang bị kiểm. Khẳng định "đã xoá" bằng chính hàm đọc
    của đối tượng bị kiểm là để nó tự chấm bài mình: một `list_roles` lọc nhầm
    sẽ làm mọi khẳng định "đã xoá" xanh mà không có gì bị xoá cả.
    """
    rows = (
        await session.execute(
            select(
                RoleAssignment.principal_user_id,
                RoleAssignment.principal_group,
                RoleAssignment.role,
            ).where(
                RoleAssignment.scope_type == scope_type,
                RoleAssignment.scope_id == scope_id,
            )
        )
    ).all()
    return {(u, g): r for u, g, r in rows}


# ------------------------------------------------------------------ quy tắc 1


async def test_member_cannot_grant_admin(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.grant(scope=("workspace", f.ws_a), role=Role.admin, user_id=f.user_alice)


async def test_member_cannot_grant_member(rbac_fixture):
    """Chặn admin thôi thì chưa đủ: member gán được member là leo thang hai
    bước thay vì một, và bước thứ hai không cần ai giúp."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.grant(scope=("workspace", f.ws_a), role=Role.member, user_id=f.user_alice)


async def test_member_cannot_promote_themselves(rbac_fixture):
    """Hình dạng thật của sự cố: `role.grant` một mình cho phép member tự nâng
    mình lên admin trong đúng một lệnh."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.grant(scope=("workspace", f.ws_a), role=Role.admin, user_id=f.user_bob)
    # Và vai trò trong bảng KHÔNG đổi. Một Forbidden ném ra SAU khi đã INSERT
    # thì thứ cứu là rollback của request, không phải quy tắc này — và một
    # request khác trong cùng transaction có thể commit hộ.
    assert (await _roles_at(f.session, "workspace", f.ws_a))[(f.user_bob, None)] == "member"


async def test_member_can_grant_contributor(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = RoleStore(f.session, f.principal_bob)
    await store.grant(scope=("workspace", f.ws_a), role=Role.contributor, user_id=f.user_alice)
    assert await store.list_roles(("workspace", f.ws_a))


async def test_member_can_grant_viewer(rbac_fixture):
    """Quy tắc 1 phải là một TRẦN, không phải một danh sách chặn. Cấm cả viewer
    là chặn oan, và test "member không gán được admin" một mình vẫn xanh nếu ai
    đó vô hiệu hoá toàn bộ đường gán."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = RoleStore(f.session, f.principal_bob)
    await store.grant(scope=("workspace", f.ws_a), role=Role.viewer, user_id=f.user_alice)
    assert (await _roles_at(f.session, "workspace", f.ws_a))[(f.user_alice, None)] == "viewer"


async def test_admin_can_grant_admin(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    await store.grant(scope=("workspace", f.ws_a), role=Role.admin, user_id=f.user_bob)
    assert (await _roles_at(f.session, "workspace", f.ws_a))[(f.user_bob, None)] == "admin"


async def test_contributor_cannot_grant_anything(rbac_fixture):
    """contributor không có `role.grant`, nên nó dừng ở cửa quyền chứ không đi
    tới quy tắc 1 — hai lớp khác nhau, và phải kiểm cả hai."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.grant(scope=("workspace", f.ws_a), role=Role.viewer, user_id=f.user_alice)


async def test_stranger_gets_404_not_403_when_granting(rbac_fixture):
    """Không thấy workspace thì nó KHÔNG TỒN TẠI với người đó. 403 ở đây là xác
    nhận nó có thật — cùng lý do với `_enforce`."""
    f = rbac_fixture
    store = RoleStore(f.session, f.principal_nobody)
    with pytest.raises(NotVisible):
        await store.grant(scope=("workspace", f.ws_a), role=Role.viewer, user_id=f.user_alice)


async def test_member_inheriting_from_domain_still_cannot_grant_admin(rbac_fixture):
    """Vai trò hiệu lực đến từ domain cũng là vai trò hiệu lực. Nếu quy tắc 1
    đọc một hàng cấp quyền trực tiếp thay vì vai trò hiệu lực thì đường này lọt."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("domain", f.domain_x), role=Role.member)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.grant(scope=("workspace", f.ws_a), role=Role.admin, user_id=f.user_alice)


async def test_grant_is_an_upsert_not_a_duplicate(rbac_fixture):
    """Gán lại cùng principal + scope là ĐỔI vai trò. Không có ON CONFLICT thì
    đây là `UniqueViolation` giữa một request bình thường."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    await store.grant(scope=("workspace", f.ws_a), role=Role.viewer, user_id=f.user_bob)
    await store.grant(scope=("workspace", f.ws_a), role=Role.contributor, user_id=f.user_bob)
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert rows[(f.user_bob, None)] == "contributor"
    assert len(rows) == 2


async def test_grant_to_a_group_is_an_upsert_too(rbac_fixture):
    """`principal_user_id` NULL cho hàng nhóm. Suy luận ON CONFLICT phải khớp
    được index `NULLS NOT DISTINCT`, nếu không lần gán thứ hai cho cùng một nhóm
    là một hàng TRÙNG chứ không phải một lần đổi vai trò."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    await store.grant(scope=("workspace", f.ws_a), role=Role.viewer, group="ops")
    await store.grant(scope=("workspace", f.ws_a), role=Role.member, group="ops")
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert rows[(None, "ops")] == "member"
    assert len(rows) == 2


async def test_grant_requires_exactly_one_of_user_or_group(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    # 422 tường minh: `Forbidden` và `LastAdminError` cũng là `HTTPException`,
    # nên bắt lớp cha thôi thì test này xanh kể cả khi code từ chối vì lý do
    # hoàn toàn khác.
    with pytest.raises(HTTPException) as e1:
        await store.grant(scope=("workspace", f.ws_a), role=Role.viewer)
    assert e1.value.status_code == 422
    with pytest.raises(HTTPException) as e2:
        await store.grant(
            scope=("workspace", f.ws_a), role=Role.viewer, user_id=f.user_bob, group="ops"
        )
    assert e2.value.status_code == 422


# ------------------------------------------------------------------ quy tắc 2


async def test_cannot_revoke_the_last_admin(rbac_fixture):
    """Tự khoá mình khỏi workspace của chính mình là chuyện sẽ xảy ra, và cách
    sửa duy nhất lúc đó là vào database bằng tay."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(LastAdminError):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)
    # Ném lỗi mà vẫn xoá thì lỗi chỉ là trang trí.
    assert (await _roles_at(f.session, "workspace", f.ws_a))[(f.user_alice, None)] == "admin"


async def test_can_revoke_an_admin_when_another_remains(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_bob)
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert (f.user_bob, None) not in rows
    assert rows[(f.user_alice, None)] == "admin"


async def test_revoking_a_non_admin_is_never_blocked(rbac_fixture):
    """Quy tắc 2 chỉ nói về admin. Chặn cả việc thu một viewer khi còn đúng một
    admin là chặn oan, và `len(admins) <= 1` một mình sẽ làm đúng thế nếu quên
    vế `being_removed`."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    store = RoleStore(f.session, f.principal_alice)
    await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_bob)
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert (f.user_bob, None) not in rows


async def test_group_admin_counts_toward_the_last_admin_check(rbac_fixture):
    """Một nhóm admin cũng là admin. Không tính nó thì xoá được admin-người cuối
    cùng trong khi vẫn còn admin-nhóm, hoặc ngược lại chặn oan."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(group="ops", scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert rows == {(None, "ops"): "admin"}


async def test_the_last_admin_group_cannot_be_revoked_either(rbac_fixture):
    """Chiều ngược lại của test trên. Nếu quy tắc 2 chỉ chạy cho `user_id` thì
    thu nhóm admin cuối cùng vẫn để workspace không còn admin nào."""
    f = rbac_fixture
    await f.grant(group="ops", scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_alice, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(LastAdminError):
        await store.revoke(scope=("workspace", f.ws_a), group="ops")
    assert (await _roles_at(f.session, "workspace", f.ws_a))[(None, "ops")] == "admin"


async def test_revoking_one_group_leaves_other_groups_alone(rbac_fixture):
    """`_one_principal` phải khớp ĐÚNG nhóm bị thu.

    Không có test này thì nhánh nhóm rút gọn thành `principal_user_id == None`
    vẫn xanh cả bộ — SQLAlchemy dịch nó thành `IS NULL`, khớp MỌI hàng nhóm
    trong phạm vi, nên thu một nhóm là xoá sạch mọi nhóm. Đã kiểm bằng
    mutation: rút gọn như thế SỐNG SÓT cho tới khi có test này."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(group="ops", scope=("workspace", f.ws_a), role=Role.viewer)
    await f.grant(group="data-eng", scope=("workspace", f.ws_a), role=Role.contributor)
    store = RoleStore(f.session, f.principal_alice)
    await store.revoke(scope=("workspace", f.ws_a), group="ops")
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert (None, "ops") not in rows
    assert rows[(None, "data-eng")] == "contributor"
    assert rows[(f.user_alice, None)] == "admin"


async def test_revoking_a_user_leaves_group_rows_alone(rbac_fixture):
    """Chiều ngược lại: `principal_group == None` cũng dịch thành `IS NULL` và
    khớp mọi hàng NGƯỜI trong phạm vi."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    await f.grant(group="ops", scope=("workspace", f.ws_a), role=Role.viewer)
    store = RoleStore(f.session, f.principal_alice)
    await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_bob)
    rows = await _roles_at(f.session, "workspace", f.ws_a)
    assert (f.user_bob, None) not in rows
    assert rows[(None, "ops")] == "viewer"
    assert rows[(f.user_alice, None)] == "admin"


async def test_an_admin_at_a_wider_scope_does_not_rescue_the_last_local_admin(rbac_fixture):
    """Admin cấp tenant KHÔNG được tính là admin của workspace này.

    Đếm cả tổ tiên thì một tenant admin (thường có, thường là một tài khoản
    vận hành) làm quy tắc 2 không bao giờ kích hoạt nữa — nó xanh mãi mãi và
    không bảo vệ gì. Quy tắc nói về admin CỦA PHẠM VI NÀY."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(LastAdminError):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)


async def test_an_admin_of_another_workspace_does_not_count(rbac_fixture):
    """`scope_id` phải nằm trong điều kiện đếm. Không có nó thì admin của bất kỳ
    workspace nào cũng cứu được workspace này."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_b), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(LastAdminError):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)


async def test_a_member_does_not_count_as_an_admin(rbac_fixture):
    """`role == 'admin'` phải nằm trong điều kiện đếm. Đếm mọi assignment thì
    một viewer bất kỳ cũng làm quy tắc 2 im lặng."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(LastAdminError):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)


async def test_last_admin_rule_applies_to_item_scope_too(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(LastAdminError):
        await store.revoke(scope=("item", f.item_a1), user_id=f.user_alice)


async def test_revoke_needs_role_grant(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)


async def test_list_roles_needs_role_read(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.list_roles(("workspace", f.ws_a))


async def test_grant_at_tenant_scope_is_refused(rbac_fixture):
    """Giai đoạn 1a không cấp được vai trò ở phạm vi tenant/domain qua API. Từ
    chối thẳng chứ không im lặng bỏ qua phép kiểm quyền."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    with pytest.raises(Forbidden):
        await store.grant(scope=("tenant", DEFAULT_TENANT_ID), role=Role.viewer, user_id=f.user_bob)
    with pytest.raises(Forbidden):
        await store.revoke(scope=("domain", f.domain_x), user_id=f.user_bob)


async def test_revoke_of_a_missing_assignment_is_a_no_op(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    store = RoleStore(f.session, f.principal_alice)
    await store.revoke(scope=("workspace", f.ws_a), user_id=uuid.uuid4())
    assert len(await _roles_at(f.session, "workspace", f.ws_a)) == 1


async def test_member_cannot_revoke_an_admin(rbac_fixture):
    """QUY TẮC 3, đối xứng với quy tắc 1: chỉ thu được vai trò mình gán được.

    Không có nó thì `GRANTABLE_BY` chặn member GÁN lên admin nhưng không gì chặn
    member THU của một admin — thứ bạn không được cho, bạn lại được lấy đi.
    Còn hai admin nên quy tắc 2 (admin cuối) KHÔNG phải là thứ chặn ở đây; test
    này cô lập đúng quy tắc 3.
    """
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(group="ops", scope=("workspace", f.ws_a), role=Role.admin)

    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)


async def test_member_cannot_revoke_another_member(rbac_fixture):
    """`member` không nằm trong GRANTABLE_BY[member], nên cũng không thu được."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.member)

    store = RoleStore(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)


async def test_member_can_still_revoke_a_contributor(rbac_fixture):
    """Chiều ngược lại — quy tắc 3 KHÔNG được chặn oan việc dọn dẹp thường ngày.
    Member tự thêm contributor thì phải tự gỡ được, nếu không admin thành nút thắt."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.contributor)

    store = RoleStore(f.session, f.principal_bob)
    await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)
    remaining = {r.principal_user_id for r in await store.list_roles(("workspace", f.ws_a))}
    assert f.user_alice not in remaining


async def test_admin_can_revoke_an_admin_when_another_remains(rbac_fixture):
    """GRANTABLE_BY[admin] là mọi vai trò, nên quy tắc 3 không cản admin."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)

    store = RoleStore(f.session, f.principal_bob)
    await store.revoke(scope=("workspace", f.ws_a), user_id=f.user_alice)
