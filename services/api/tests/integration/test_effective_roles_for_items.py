"""`PermissionService.effective_roles_for_items` — bản theo lô cho
`POST /internal/authz/items`.

Hai tính chất, và mỗi cái đỏ vì một lý do khác hẳn nếu mất:

1. **Đối chiếu với đường đơn.** Bản theo lô dùng lại `_chain_conditions`
   (xem `permissions.py`), không viết lại chuỗi tổ tiên. Nếu ai đó viết lại nó
   — dù chỉ để "tối ưu" — hai đường có thể lệch nhau đúng ở những cấu hình mà
   `test_batch_matches_single_item_lookup` rải ra: assignment ở bốn cấp scope
   khác nhau, cộng trường hợp không có gì. Lệch ở đây nghĩa là `loom-query` cho
   người dùng đọc một bảng họ không được phép đọc.
2. **Một round trip cho cả lô.** Đếm bằng `sql_log` — câu lệnh THẬT gửi tới
   Postgres — không bằng `PermissionService.query_count`: biến đó chỉ chứng
   minh dòng `self.query_count += 1` đã chạy, mù hoàn toàn với việc hàm có lặp
   N vòng gọi `_query_item_roles` bên dưới hay không.
"""

import uuid

import pytest

from loom_api.models import DEFAULT_TENANT_ID
from loom_api.permissions import PermissionService
from loom_core.roles import Role

from .conftest import RbacFixture

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param(None, id="no-grant"),
        pytest.param("item", id="item-scope"),
        pytest.param("workspace", id="workspace-scope"),
        pytest.param("domain", id="domain-scope"),
        pytest.param("tenant", id="tenant-scope"),
    ],
)
async def test_batch_matches_single_item_lookup(
    rbac_fixture: RbacFixture, scope: str | None
) -> None:
    f = rbac_fixture
    if scope == "item":
        await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.viewer)
    elif scope == "workspace":
        await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.contributor)
    elif scope == "domain":
        await f.grant(user=f.user_alice, scope=("domain", f.domain_x), role=Role.member)
    elif scope == "tenant":
        await f.grant(user=f.user_alice, scope=("tenant", DEFAULT_TENANT_ID), role=Role.admin)
    # scope is None: cố ý không gán gì — "không có quyền" cũng phải khớp nhau
    # giữa hai đường, không chỉ trường hợp có quyền.

    # Item cuối KHÔNG tồn tại: đường theo lô và đường đơn phải đồng ý CẢ ở đó.
    item_ids = (f.item_a1, f.item_a2, f.item_b1, uuid.uuid4())

    batch_perms = PermissionService(f.session, f.principal_alice)
    via_batch = await batch_perms.effective_roles_for_items(item_ids)

    # Một `PermissionService` RIÊNG cho vế đơn: cache trong thực thể kia đã bị
    # lô nạp đầy, và so một bên có cache với một bên không có cache là so hai
    # đường KHÁC NHAU thay vì so cùng một câu hỏi hai cách.
    single_perms = PermissionService(f.session, f.principal_alice)
    via_single = {
        item_id: await single_perms.effective_role_for_item(item_id) for item_id in item_ids
    }

    assert via_batch == via_single, f"scope={scope}: đường lô và đường đơn lệch nhau"


async def test_batch_is_one_round_trip_for_many_items(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_a), role=Role.viewer)
    perms = PermissionService(f.session, f.principal_alice)

    item_ids = (f.item_a1, f.item_a2, f.item_b1)
    mark = len(f.sql_log)
    roles = await perms.effective_roles_for_items(item_ids)
    statements = f.statements_since(mark)

    assert len(statements) == 1, (
        f"lô {len(item_ids)} item phải là MỘT round trip, thấy {len(statements)}: {statements}"
    )
    assert roles == {f.item_a1: Role.viewer, f.item_a2: Role.viewer, f.item_b1: None}


async def test_batch_reuses_the_per_item_cache_in_both_directions(
    rbac_fixture: RbacFixture,
) -> None:
    """Hỏi lẻ trước rồi hỏi lô, và ngược lại: cả hai chiều đều không được tốn
    thêm round trip cho item đã biết câu trả lời."""
    f = rbac_fixture
    await f.grant(user=f.user_alice, scope=("item", f.item_a1), role=Role.admin)
    perms = PermissionService(f.session, f.principal_alice)

    assert await perms.effective_role_for_item(f.item_a1) is Role.admin

    mark = len(f.sql_log)
    roles = await perms.effective_roles_for_items((f.item_a1, f.item_a2))
    # item_a1 đã cache: chỉ item_a2 cần một round trip.
    assert len(f.statements_since(mark)) == 1
    assert roles == {f.item_a1: Role.admin, f.item_a2: None}

    mark = len(f.sql_log)
    assert await perms.effective_role_for_item(f.item_a2) is None
    assert f.statements_since(mark) == [], "item_a2 đã được lô nạp cache, không cần hỏi lại"
