"""Nửa thứ hai của "hai đường, một nguồn quy tắc": biểu thức lọc cho danh sách.

`test_permissions_differential.py` khẳng định hai đường ĐỒNG Ý. File này khẳng
định đường danh sách nói ĐÚNG — hai chuyện khác nhau: hai đường cùng sai giống
nhau vẫn đồng ý. Ở đây mỗi test nêu một câu cụ thể về những hàng nào được trả.
"""

import uuid

import pytest
from sqlalchemy import Select, update
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.models import DEFAULT_TENANT_ID, DELETED, Item, Workspace
from loom_api.permissions import (
    _roles_allowing,
    visible_items_select,
    visible_workspaces_select,
)
from loom_core.roles import ACTION_MATRIX, Action, Role

from .conftest import RbacFixture

pytestmark = pytest.mark.integration


async def ids(
    session: AsyncSession, stmt: Select[tuple[Item]] | Select[tuple[Workspace]]
) -> set[uuid.UUID]:
    return {row.id for row in (await session.execute(stmt)).scalars().all()}


async def _soft_delete(
    session: AsyncSession, model: type[Item | Workspace], row: uuid.UUID
) -> None:
    await session.execute(update(model).where(model.id == row).values(state=DELETED))
    await session.flush()


async def test_no_assignment_sees_nothing(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    assert await ids(f.session, visible_items_select(f.principal_bob)) == set()
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == set()


async def test_workspace_assignment_shows_every_item_inside(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == {f.item_a1, f.item_a2}


async def test_item_assignment_shows_only_that_item(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_a1), role=Role.viewer)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == {f.item_a1}


async def test_item_assignment_also_reveals_the_containing_workspace(
    rbac_fixture: RbacFixture,
) -> None:
    """Nếu ai đó chia sẻ lẻ một item cho bạn, bạn phải thấy workspace chứa nó —
    không thì item đó không có đường nào tới. Spec mục 4.3."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_a1), role=Role.viewer)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_a}


async def test_group_assignment_works_in_the_predicate_too(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(group="data-eng", scope=("workspace", f.ws_b), role=Role.viewer)
    # alice thuộc data-eng, bob không
    assert await ids(f.session, visible_items_select(f.principal_alice)) == {f.item_b1}
    assert await ids(f.session, visible_items_select(f.principal_bob)) == set()


async def test_domain_assignment_reaches_only_workspaces_in_that_domain(
    rbac_fixture: RbacFixture,
) -> None:
    """Nhánh `domain` là lý do hàm trả CẢ CÂU SELECT: nó đọc `Workspace.domain_id`,
    nên truy vấn buộc phải join Workspace. Nếu hàm chỉ trả về một điều kiện rời
    thì người gọi quên join là cartesian product — mọi item của mọi workspace.

    ws_a thuộc domain_x; ws_b không thuộc domain nào. Thêm ws_c ở một domain
    KHÁC: không có nó thì test chỉ chứng minh `scope_id = NULL` không khớp, mà
    điều đó đúng trong SQL bất kể vế trái là gì.
    """
    f = rbac_fixture
    domain_y = await f.add_domain("domain-y")
    ws_c = await f.add_workspace("ws-c", domain_id=domain_y)
    await f.add_item(ws_c, "item-c1")

    await f.grant(user=f.user_bob, scope=("domain", f.domain_x), role=Role.viewer)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == {f.item_a1, f.item_a2}
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_a}


async def test_workspace_filter_narrows_without_widening_permission(
    rbac_fixture: RbacFixture,
) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("tenant", DEFAULT_TENANT_ID), role=Role.viewer)
    everything = await ids(f.session, visible_items_select(f.principal_bob))
    assert everything == {f.item_a1, f.item_a2, f.item_b1}
    only_a = await ids(f.session, visible_items_select(f.principal_bob, workspace_id=f.ws_a))
    assert only_a == {f.item_a1, f.item_a2}


async def test_the_workspace_filter_cannot_reveal_what_permission_hides(
    rbac_fixture: RbacFixture,
) -> None:
    """Đối chứng cho test trên: tham số `workspace_id` chỉ THU HẸP. Một cài đặt
    thay điều kiện quyền bằng bộ lọc workspace (thay vì thêm vào) vẫn xanh ở test
    kia — ở đó người dùng có quyền tenant nên hai cách cho cùng kết quả."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    assert await ids(f.session, visible_items_select(f.principal_bob, workspace_id=f.ws_b)) == set()


async def test_soft_deleted_items_are_invisible(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    await _soft_delete(f.session, Item, f.item_a1)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == {f.item_a2}


async def test_a_soft_deleted_workspace_hides_the_items_inside_it(
    rbac_fixture: RbacFixture,
) -> None:
    """`Item.state` và `Workspace.state` là HAI bộ lọc. Item vẫn `active`; chỉ
    workspace bị xoá mềm — bỏ vế `Workspace.state == ACTIVE` thì item của một
    workspace đã xoá vẫn hiện ra trong danh sách."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("tenant", DEFAULT_TENANT_ID), role=Role.viewer)
    await _soft_delete(f.session, Workspace, f.ws_a)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == {f.item_b1}


async def test_a_soft_deleted_workspace_is_not_listed(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("tenant", DEFAULT_TENANT_ID), role=Role.viewer)
    await _soft_delete(f.session, Workspace, f.ws_a)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_b}


async def test_a_soft_deleted_item_does_not_reveal_its_workspace(
    rbac_fixture: RbacFixture,
) -> None:
    """Nhánh "item được chia sẻ lẻ" có bộ lọc `state` RIÊNG của nó. Không có vế
    đó, một item đã xoá mềm vẫn kéo workspace của nó hiện ra — và workspace đó
    mở ra thì rỗng, vì `visible_items_select` đã lọc item ấy đi rồi."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_a1), role=Role.viewer)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_a}
    await _soft_delete(f.session, Item, f.item_a1)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == set()


async def test_an_item_grant_reveals_the_workspace_but_not_its_siblings(
    rbac_fixture: RbacFixture,
) -> None:
    """Hai vế trong một test có chủ đích: nhánh `by_item_inside` là chỗ dễ viết
    quá rộng nhất. Thấy workspace, nhưng CHỈ thấy đúng item được chia sẻ."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_a1), role=Role.viewer)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_a}
    assert await ids(f.session, visible_items_select(f.principal_bob)) == {f.item_a1}


async def test_an_item_grant_in_one_workspace_does_not_reveal_another(
    rbac_fixture: RbacFixture,
) -> None:
    """`by_item_inside` phải nối `Item.workspace_id == Workspace.id`. Bỏ vế đó
    thì MỌI workspace hiện ra ngay khi principal có một grant cấp item bất kỳ."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_b1), role=Role.viewer)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_b}


async def test_a_grant_whose_scope_type_lies_confers_nothing_here_either(
    rbac_fixture: RbacFixture,
) -> None:
    """`scope_id` không có foreign key, nên một hàng nói "item" mà mang id của
    một workspace là hàng database chấp nhận. Điều kiện `scope_type` trong từng
    nhánh là thứ duy nhất chặn nó cấp quyền ở nhầm tầng — và biểu thức lọc danh
    sách phải chặn y hệt đường một-tài-nguyên (`test_permissions.py` có bản đối
    ứng của test này)."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("item", f.domain_x), role=Role.admin)
    await f.grant(user=f.user_bob, scope=("workspace", f.item_a1), role=Role.admin)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == set()
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == set()


async def test_a_tenant_grant_does_not_reach_an_item_stamped_with_another_tenant(
    rbac_fixture: RbacFixture,
) -> None:
    """Nhánh `tenant` đọc `Item.tenant_id`, KHÔNG `Workspace.tenant_id`.

    `item.tenant_id` không có foreign key và là bản sao denormalise của tenant
    của workspace, nên hai cột LỆCH nhau là trạng thái database cho phép tồn tại.
    Đọc theo item là hỏng KÍN (grant toàn tenant không với tới hàng lệch); đọc
    theo workspace là hỏng MỞ. `test_permissions.py` chốt lựa chọn đó cho đường
    một-tài-nguyên — và chỉ cho đường đó.

    Không có test này, đổi `Item.tenant_id` thành `Workspace.tenant_id` CHỈ ở
    biểu thức lọc danh sách đi qua trọn cả ba file test quyền (đã kiểm bằng
    mutation: mutant sống sót). Test đối chiếu cũng không thấy: thế giới ngẫu
    nhiên của nó không sinh hàng lệch tenant, mà một hàng như thế thì cả hai
    đường cùng đọc sai theo cùng một cách.
    """
    f = rbac_fixture
    stray = await f.add_item(f.ws_a, "item-stray", tenant_id=uuid.uuid4())
    await f.grant(user=f.user_bob, scope=("tenant", DEFAULT_TENANT_ID), role=Role.viewer)
    visible = await ids(f.session, visible_items_select(f.principal_bob))
    assert stray not in visible
    assert visible == {f.item_a1, f.item_a2, f.item_b1}


async def test_a_role_without_read_permission_sees_nothing(
    rbac_fixture: RbacFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bài kiểm HÀNH VI cho dòng `RoleAssignment.role.in_(_roles_allowing(...))`.

    Hôm nay cả bốn vai trò đều có `item_read`, nên bỏ hẳn dòng đó KHÔNG làm test
    nào đỏ — đã kiểm bằng mutation, mutant sống sót cả ba file test quyền, và
    test đối chiếu của Task 12 cũng để nó đi qua. Hai đường thật sự tương đương
    với ma trận hiện tại; cái mất là "suy ra từ ACTION_MATRIX", và mất nó thì
    NGÀY THÊM vai trò mới là ngày danh sách trả về hàng `require_item` từ chối.

    Không thêm được một vai trò thứ năm để kiểm: `ck_role_assignment_role` chốt
    cột `role` vào đúng bốn tên, nên một hàng mang vai trò lạ không chèn nổi —
    một probe thêm thành viên vào `enum Role` chết bằng IntegrityError, tức là
    đỏ vì một lý do không liên quan.

    Còn đúng một cách: lấy `item_read` KHỎI một vai trò có thật, trong phạm vi
    test. `allows()` đọc `ACTION_MATRIX` lúc gọi và `_roles_allowing` chạy lúc
    DỰNG câu select, nên đây đúng là tình huống "có một vai trò không cho đọc"
    mà Task 11 nói tới, chỉ khác là nó tự dọn sau khi chạy.
    """
    monkeypatch.setitem(ACTION_MATRIX, Role.viewer, frozenset())
    assert "viewer" not in _roles_allowing(Action.item_read)

    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    assert await ids(f.session, visible_items_select(f.principal_bob)) == set()
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == set()

    # Đối chứng, và nó bắt buộc: một biểu thức lọc hỏng toàn diện — trả rỗng cho
    # mọi người — cũng làm ba dòng trên xanh.
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.contributor)
    assert await ids(f.session, visible_items_select(f.principal_alice)) == {f.item_a1, f.item_a2}


async def test_a_workspace_is_listed_for_a_role_that_reads_workspaces_but_not_items(
    rbac_fixture: RbacFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hai nhánh của `visible_workspaces_select` lọc theo HAI hành động khác nhau:
    `by_scope` theo `workspace_read`, `by_item_inside` theo `item_read`.

    Với ma trận hôm nay hai tập vai trò trùng nhau, nên đổi `workspace_read`
    thành `item_read` ở `by_scope` là một mutant TƯƠNG ĐƯƠNG: câu SQL sinh ra
    giống hệt từng byte và không phép thử hành vi nào phân biệt nổi (đã kiểm).

    Nó thôi tương đương ngay khi có một vai trò thấy được workspace mà không đọc
    được item bên trong. Vai trò đó hoàn toàn hợp lệ — nó KHÔNG phá tiền đề đơn
    điệu, cũng không phá tiền đề `roles_allowing(item_read) ⊆
    roles_allowing(workspace_read)` mà test đối chiếu dựa vào. Dựng đúng nó ở đây
    để dòng `workspace_read` có một bài kiểm thay vì một sự trùng hợp.
    """
    monkeypatch.setitem(ACTION_MATRIX, Role.viewer, frozenset({Action.workspace_read}))
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    assert await ids(f.session, visible_workspaces_select(f.principal_bob)) == {f.ws_a}
    assert await ids(f.session, visible_items_select(f.principal_bob)) == set()


def test_the_role_filter_is_derived_from_the_action_matrix() -> None:
    """`_roles_allowing` phải ĐỌC `ACTION_MATRIX`, không phải trả về mọi vai trò.

    Với ma trận hôm nay cả bốn vai trò đều có `item_read`, nên mọi test dùng
    `item_read` ở trên đều xanh với một cài đặt `return [str(r) for r in Role]`.
    Hỏi bằng một hành động mà CHỈ admin có là cách duy nhất để câu khẳng định này
    có khả năng sai.
    """
    assert _roles_allowing(Action.domain_manage) == ["admin"]
    assert _roles_allowing(Action.workspace_delete) == ["admin"]
    assert _roles_allowing(Action.item_read) == [
        str(role) for role in Role if Action.item_read in ACTION_MATRIX[role]
    ]
