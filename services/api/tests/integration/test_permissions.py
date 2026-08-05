"""RBAC chỉ kiểm được với Postgres thật: cả hai đường đánh giá là SQL.

Mỗi test tự gán đúng những assignment nó cần — `rbac_fixture` cố ý KHÔNG gán gì.
Một fixture "đã có sẵn vài quyền" làm mọi test đọc được hai cách, và cách sai
luôn là cách trông đúng.
"""

import uuid

import pytest

from loom_api.models import DEFAULT_TENANT_ID
from loom_api.permissions import Forbidden, NotVisible, PermissionService
from loom_core.roles import Action, Role
from loom_core.schemas import Principal

from .conftest import RbacFixture

pytestmark = pytest.mark.integration


async def test_no_assignment_means_no_role(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    perms = PermissionService(f.session, f.principal_nobody)
    assert await perms.effective_role_for_item(f.item_a1) is None


async def test_workspace_role_reaches_items_inside(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.contributor)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.contributor
    # Và KHÔNG với tới workspace khác. Không có vế này thì một nhánh workspace
    # bỏ mất điều kiện `scope_id == workspace_id` vẫn xanh.
    assert await perms.effective_role_for_item(f.item_b1) is None


async def test_a_grant_to_one_user_is_not_a_grant_to_another(rbac_fixture: RbacFixture) -> None:
    """`principal_matches` khớp theo `principal_user_id`. Nếu nó khớp theo bất
    cứ thứ gì khác — hay không khớp gì cả — thì bob đọc được quyền của alice."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    alice = PermissionService(f.session, f.principal_alice)
    bob = PermissionService(f.session, f.principal_bob)
    assert await alice.effective_role_for_item(f.item_a1) is Role.admin
    assert await bob.effective_role_for_item(f.item_a1) is None


async def test_item_role_does_not_leak_to_siblings(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.viewer)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.viewer
    assert await perms.effective_role_for_item(f.item_a2) is None


async def test_item_role_does_not_climb_to_the_workspace(rbac_fixture: RbacFixture) -> None:
    """Chuỗi tổ tiên chạy một chiều: từ tài nguyên LÊN. Một quyền trên item
    không phải là quyền trên workspace chứa nó — nếu leo ngược được thì chia sẻ
    lẻ một item là trao cả workspace."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.admin
    assert await perms.effective_role_for_workspace(f.ws_a) is None


async def test_highest_role_on_the_chain_wins(rbac_fixture: RbacFixture) -> None:
    """max() trên chuỗi tổ tiên. Gán viewer ở workspace và admin ở item thì kết
    quả là admin — assignment chỉ THÊM quyền, không trừ."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.viewer)
    await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.admin


async def test_a_lower_item_role_does_not_pull_down_a_higher_workspace_role(
    rbac_fixture: RbacFixture,
) -> None:
    """Chiều ngược lại của test trên. `max()` phải thật sự là max, không phải
    "cái hẹp nhất thắng" — một cài đặt lấy vai trò của scope cụ thể nhất sẽ xanh
    ở test kia và đỏ ở đây."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.viewer)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.admin


async def test_group_assignment_applies_to_member_of_that_group(
    rbac_fixture: RbacFixture,
) -> None:
    f = rbac_fixture
    await f.grant(group="data-eng", scope=("workspace", f.ws_a), role=Role.member)
    # principal_alice thuộc nhóm data-eng
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.member
    # bob không thuộc nhóm nào
    perms_bob = PermissionService(f.session, f.principal_bob)
    assert await perms_bob.effective_role_for_item(f.item_a1) is None


async def test_group_assignment_does_not_reach_a_different_group(
    rbac_fixture: RbacFixture,
) -> None:
    """bob KHÔNG có nhóm nào, nên với bob mệnh đề `principal_group IN (...)`
    biến mất hoàn toàn khỏi câu SQL — test trên không phân biệt được "lọc theo
    tên nhóm" với "chỉ cần có nhóm bất kỳ". Người này CÓ nhóm, nhóm khác."""
    f = rbac_fixture
    await f.grant(group="data-eng", scope=("workspace", f.ws_a), role=Role.admin)
    in_another_group = Principal(
        user_id=f.user_bob,
        subject="bob",
        email="bob@loom.local",
        display_name="bob",
        groups=("finance",),
    )
    perms = PermissionService(f.session, in_another_group)
    assert await perms.effective_role_for_item(f.item_a1) is None


async def test_user_and_group_roles_are_combined_by_max(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(group="data-eng", scope=("workspace", f.ws_a), role=Role.viewer)
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.member)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.member


async def test_tenant_role_reaches_everything(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.admin
    assert await perms.effective_role_for_item(f.item_b1) is Role.admin
    assert await perms.effective_role_for_workspace(f.ws_b) is Role.admin


async def test_a_tenant_grant_does_not_reach_an_item_stamped_with_another_tenant(
    rbac_fixture: RbacFixture,
) -> None:
    """`item.tenant_id` KHÔNG có foreign key (models.py nói rõ) và là bản sao
    denormalise của tenant của workspace. Nhánh tenant đọc `Item.tenant_id` chứ
    không `Workspace.tenant_id`, và lựa chọn đó có hậu quả: nếu hai cột từng
    lệch nhau thì hàng đó hỏng KÍN — một grant toàn tenant không với tới nó.

    Đọc theo workspace là hỏng MỞ, và một dòng bình luận nói "chọn hỏng kín" mà
    không có test nào đi qua nó thì chỉ là một lời tuyên bố.
    """
    f = rbac_fixture
    stray = await f.add_item(f.ws_a, "item-stray", tenant_id=uuid.uuid4())
    await f.grant(user=f.user_alice, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)

    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.admin
    assert await perms.effective_role_for_item(stray) is None


async def test_domain_role_reaches_workspaces_in_that_domain_only(
    rbac_fixture: RbacFixture,
) -> None:
    """ws_a thuộc domain_x, ws_b không thuộc domain nào."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("domain", f.domain_x), role=Role.member)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.member
    assert await perms.effective_role_for_item(f.item_b1) is None


async def test_domain_role_does_not_reach_a_workspace_in_another_domain(
    rbac_fixture: RbacFixture,
) -> None:
    """ws_b có `domain_id IS NULL`, nên test trên chỉ chứng minh rằng NULL không
    khớp — và `scope_id = NULL` không bao giờ đúng trong SQL bất kể vế trái là
    gì. Đây là trường hợp thật: một domain KHÁC, có giá trị hẳn hoi."""
    f = rbac_fixture
    domain_y = await f.add_domain("domain-y")
    ws_c = await f.add_workspace("ws-c", domain_id=domain_y)
    item_c1 = await f.add_item(ws_c, "item-c1")

    await f.grant(user=f.user_alice, scope=("domain", f.domain_x), role=Role.member)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.member
    assert await perms.effective_role_for_item(item_c1) is None
    assert await perms.effective_role_for_workspace(ws_c) is None


async def test_a_grant_whose_scope_type_lies_about_its_scope_id_confers_nothing(
    rbac_fixture: RbacFixture,
) -> None:
    """`role_assignment.scope_id` KHÔNG có foreign key — nó trỏ tới một trong
    bốn bảng tuỳ `scope_type`, nên không có gì ở tầng database từ chối một hàng
    nói "item" mà mang id của một workspace, một domain hay của tenant. Điều
    kiện `scope_type = ...` trong TỪNG nhánh là thứ duy nhất chặn một hàng như
    thế cấp quyền ở nhầm tầng.

    Bốn hàng dưới đây phủ cả bốn nhánh: bỏ điều kiện `scope_type` ở bất kỳ nhánh
    nào cũng làm một trong ba câu khẳng định đỏ.
    """
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("item", f.ws_a), role=Role.admin)
    await f.grant(user=f.user_alice, scope=("item", f.domain_x), role=Role.admin)
    await f.grant(user=f.user_alice, scope=("item", DEFAULT_TENANT_ID), role=Role.admin)
    await f.grant(user=f.user_alice, scope=("workspace", f.item_b1), role=Role.admin)

    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_workspace(f.ws_a) is None
    assert await perms.effective_role_for_item(f.item_a1) is None
    assert await perms.effective_role_for_item(f.item_b1) is None


async def test_an_unknown_resource_id_has_no_role(rbac_fixture: RbacFixture) -> None:
    """Kể cả với một người có quyền tenant. Không có hàng item thì JOIN không ra
    hàng nào — và đó là điều làm `require_item` trả 404 cho id không tồn tại mà
    không cần một truy vấn "tồn tại không" riêng."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(uuid.uuid4()) is None
    assert await perms.effective_role_for_workspace(uuid.uuid4()) is None


async def test_one_query_per_resource_thanks_to_cache(rbac_fixture: RbacFixture) -> None:
    """Cache trong phạm vi request. Không có nó, một trang 50 item gọi require()
    50 lần và mỗi lần là một round trip."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.viewer)
    perms = PermissionService(f.session, f.principal_alice)
    await perms.effective_role_for_item(f.item_a1)
    before = perms.query_count
    await perms.effective_role_for_item(f.item_a1)
    assert perms.query_count == before, "lần thứ hai phải lấy từ cache"


async def test_a_check_really_costs_one_round_trip_and_a_repeat_costs_none(
    rbac_fixture: RbacFixture,
) -> None:
    """`query_count` là con số do chính code bị kiểm tự tăng: nó chứng minh dòng
    `+= 1` đã chạy, không chứng minh có đúng một câu lệnh đi tới Postgres. Ở đây
    đếm câu lệnh THẬT qua `before_cursor_execute`.

    Bốn tầng cố định vừa trong MỘT câu JOIN là toàn bộ luận điểm của Task 9 —
    nếu nó thành bốn truy vấn nối tiếp, hay một recursive CTE chạy nhiều vòng,
    con số dưới đây đổi và không có test nào khác thấy.
    """
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.viewer)
    perms = PermissionService(f.session, f.principal_alice)

    mark = len(f.sql_log)
    assert await perms.effective_role_for_item(f.item_a1) is Role.viewer
    first = f.statements_since(mark)
    assert len(first) == 1, f"một tài nguyên phải là MỘT round trip, thấy {len(first)}: {first}"

    mark = len(f.sql_log)
    assert await perms.effective_role_for_item(f.item_a1) is Role.viewer
    assert f.statements_since(mark) == [], "lần thứ hai không được chạm database"


async def test_the_cache_answers_for_the_resource_that_was_asked_about(
    rbac_fixture: RbacFixture,
) -> None:
    """Phép đếm truy vấn một mình KHÔNG chứng minh cache đúng: một cache trả
    `None` cho mọi lần trúng, hay một cache chỉ có một ô, đều làm nó xanh.

    Ở đây: xen kẽ các tài nguyên có câu trả lời KHÁC nhau, và đọc lại cái đầu
    tiên sau khi đã hỏi cái thứ hai.
    """
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.viewer)
    await f.grant(user=f.user_alice, scope=("item", f.item_a2), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)

    assert await perms.effective_role_for_item(f.item_a1) is Role.viewer
    assert await perms.effective_role_for_item(f.item_a2) is Role.admin
    assert await perms.effective_role_for_item(f.item_b1) is None
    # Đọc lại, giờ toàn bộ từ cache. Một ô cache duy nhất sẽ trả Role.admin hoặc
    # None cho item_a1 ở đây.
    assert await perms.effective_role_for_item(f.item_a1) is Role.viewer
    assert await perms.effective_role_for_item(f.item_a2) is Role.admin
    assert await perms.effective_role_for_item(f.item_b1) is None
    assert await perms.effective_role_for_workspace(f.ws_a) is Role.viewer
    assert await perms.effective_role_for_workspace(f.ws_b) is None


async def test_the_cache_keeps_items_and_workspaces_in_separate_namespaces(
    rbac_fixture: RbacFixture,
) -> None:
    """Khoá cache là `(loại, id)`, không phải chỉ `id`. Hai UUID không bao giờ
    trùng nhau, nên phần "loại" chỉ có tác dụng khi CÙNG một UUID được hỏi ở hai
    vai — đúng thứ xảy ra khi một handler truyền nhầm id vào `require_workspace`.

    Câu trả lời đúng cho một id sai là "không thấy". Một cache không phân vùng
    trả về quyền của tài nguyên KHÁC, tức là bịa ra một quyền chưa từng được cấp.
    """
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)
    assert await perms.effective_role_for_item(f.item_a1) is Role.admin
    assert await perms.effective_role_for_workspace(f.item_a1) is None


async def test_a_grant_made_after_a_cached_answer_is_visible_to_the_next_request(
    rbac_fixture: RbacFixture,
) -> None:
    """Cache là PHẠM VI REQUEST, và đó là một đánh đổi có tên: trong một request
    câu trả lời bị đóng băng — kể cả khi chính request đó vừa ghi một assignment
    mới. Ranh giới phải nằm ở `PermissionService`, không xa hơn.

    Nếu cache trôi lên module (một dict toàn cục, một `lru_cache`) thì vế cuối
    của test này đỏ, và trên production nó có nghĩa là thu hồi quyền không có
    hiệu lực cho tới khi tiến trình khởi động lại.
    """
    f = rbac_fixture
    stale = PermissionService(f.session, f.principal_bob)
    assert await stale.effective_role_for_item(f.item_a1) is None

    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)

    assert await stale.effective_role_for_item(f.item_a1) is None, (
        "trong CÙNG một request câu trả lời phải giữ nguyên — đó là ý nghĩa của cache"
    )
    fresh = PermissionService(f.session, f.principal_bob)
    assert await fresh.effective_role_for_item(f.item_a1) is Role.member


async def test_two_services_in_one_request_do_not_share_a_cache(
    rbac_fixture: RbacFixture,
) -> None:
    """Đối chứng cho test trên, và là thứ chặn cache trôi thành trạng thái toàn
    cục: hai principal khác nhau phải có hai câu trả lời khác nhau cho cùng một
    item, kể cả khi hỏi liên tiếp."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    alice = PermissionService(f.session, f.principal_alice)
    bob = PermissionService(f.session, f.principal_bob)
    assert await alice.effective_role_for_item(f.item_a1) is Role.admin
    assert await bob.effective_role_for_item(f.item_a1) is None
    assert await alice.effective_role_for_item(f.item_a1) is Role.admin


# ------------------------------------------------- Task 10: require(), 404 vs 403


async def test_require_raises_not_visible_when_no_read_access(rbac_fixture: RbacFixture) -> None:
    """404 chứ không 403: trả 403 cho item không được đọc là tiết lộ sự tồn tại
    của nó, mà tên item thường mang thông tin."""
    f = rbac_fixture
    perms = PermissionService(f.session, f.principal_bob)
    with pytest.raises(NotVisible) as exc:
        await perms.require_item(f.item_a1, Action.item_read)
    assert exc.value.status_code == 404


async def test_require_raises_not_visible_for_an_action_the_role_would_not_allow_either(
    rbac_fixture: RbacFixture,
) -> None:
    """Thứ tự hai nhánh: KHÔNG CÓ VAI TRÒ được trả lời trước, kể cả khi hành
    động được hỏi cũng là hành động mà một vai trò bất kỳ cũng không đủ.

    Gộp hai nhánh thành `if role is None or not allows(...): raise Forbidden`
    trông như một phép rút gọn và làm chính câu này trả 403 — tức là xác nhận
    item có thật cho một người không được phép biết điều đó.
    """
    f = rbac_fixture
    perms = PermissionService(f.session, f.principal_bob)
    with pytest.raises(NotVisible) as exc:
        await perms.require_item(f.item_a1, Action.item_delete)
    assert exc.value.status_code == 404


async def test_require_raises_not_visible_for_an_id_that_does_not_exist(
    rbac_fixture: RbacFixture,
) -> None:
    """Và một id không tồn tại phải KHÔNG phân biệt được với một id tồn tại mà
    người này không được đọc — cùng ngoại lệ, cùng câu chữ. Hai câu trả lời khác
    nhau ở đây là một oracle dò id."""
    f = rbac_fixture
    perms = PermissionService(f.session, f.principal_bob)
    with pytest.raises(NotVisible) as unknown:
        await perms.require_item(uuid.uuid4(), Action.item_read)
    with pytest.raises(NotVisible) as hidden:
        await perms.require_item(f.item_a1, Action.item_read)
    assert (unknown.value.status_code, unknown.value.detail) == (
        hidden.value.status_code,
        hidden.value.detail,
    )


async def test_require_raises_forbidden_when_readable_but_not_allowed(
    rbac_fixture: RbacFixture,
) -> None:
    """403 chỉ khi ĐỌC ĐƯỢC nhưng không được làm hành động đó. viewer thấy item
    nhưng không sửa được — nói 404 ở đây là gây hiểu nhầm là item không tồn tại."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_a1), role=Role.viewer)
    perms = PermissionService(f.session, f.principal_bob)
    await perms.require_item(f.item_a1, Action.item_read)  # không raise
    with pytest.raises(Forbidden) as exc:
        await perms.require_item(f.item_a1, Action.item_update)
    assert exc.value.status_code == 403


async def test_require_returns_the_effective_role(rbac_fixture: RbacFixture) -> None:
    """Giá trị trả về không phải trang trí: Giai đoạn 1b quyết định trường nào
    được ghi ra dựa trên vai trò mà `require_*` vừa trả. Một cài đặt trả hằng số
    `Role.viewer` đi qua mọi test chỉ kiểm ngoại lệ."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    perms = PermissionService(f.session, f.principal_bob)
    assert await perms.require_item(f.item_a1, Action.item_read) is Role.member
    assert await perms.require_workspace(f.ws_a, Action.workspace_read) is Role.member

    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.admin)
    alice = PermissionService(f.session, f.principal_alice)
    assert await alice.require_item(f.item_a1, Action.item_read) is Role.admin


async def test_require_checks_the_action_it_was_given(rbac_fixture: RbacFixture) -> None:
    """`allows(role, action)` phải đọc ĐÚNG hành động được truyền vào. Một cài
    đặt luôn hỏi `Action.item_read` cho phép mọi vai trò làm mọi việc, và mọi
    test chỉ dùng item_read vẫn xanh."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    perms = PermissionService(f.session, f.principal_bob)
    # contributor: sửa item được, sửa workspace thì không.
    assert await perms.require_item(f.item_a1, Action.item_update) is Role.contributor
    with pytest.raises(Forbidden):
        await perms.require_workspace(f.ws_a, Action.workspace_update)


async def test_require_workspace_follows_the_same_rule(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    perms_bob = PermissionService(f.session, f.principal_bob)
    with pytest.raises(NotVisible):
        await perms_bob.require_workspace(f.ws_a, Action.workspace_read)

    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    # Thực thể MỚI: `perms_bob` đã cache "không có vai trò" cho ws_a, và cache
    # là phạm vi request có chủ đích. Xem
    # test_a_grant_made_after_a_cached_answer_is_visible_to_the_next_request.
    perms = PermissionService(f.session, f.principal_bob)
    await perms.require_workspace(f.ws_a, Action.workspace_update)
    with pytest.raises(Forbidden):
        await perms.require_workspace(f.ws_a, Action.workspace_delete)


async def test_require_asks_the_right_question_for_each_resource_kind(
    rbac_fixture: RbacFixture,
) -> None:
    """`require_workspace` phải đi qua `effective_role_for_workspace`. Nếu nó
    gọi nhầm sang đường item thì một quyền trên item leo lên thành quyền trên
    workspace — và ngược lại, một workspace id đi vào truy vấn item không khớp
    hàng nào nên mọi thứ thành 404, một lỗi dễ thấy hơn nhiều."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("item", f.item_a1), role=Role.admin)
    perms = PermissionService(f.session, f.principal_bob)
    assert await perms.require_item(f.item_a1, Action.item_read) is Role.admin
    with pytest.raises(NotVisible):
        await perms.require_workspace(f.ws_a, Action.workspace_read)
