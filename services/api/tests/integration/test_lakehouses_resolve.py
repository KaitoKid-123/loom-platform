"""`POST /internal/lakehouses/resolve` — dịch `name` sang `id` cho `loom-query`.

Gọi THẲNG hàm handler (`resolve_lakehouses`), không qua HTTP: cùng lý do
`test_effective_roles_for_items.py` đo bằng `sql_log` trên `rbac_fixture.
session` — `api_world` dựng một `Database`/engine RIÊNG cho app thật, khác
`db_engine` mà `sql_log` lắng nghe, nên đếm round trip chỉ đúng khi test chạy
trên CÙNG session/engine với `sql_log`. `test_the_endpoint_is_reachable_over_
http`/`test_the_endpoint_is_not_reachable_under_api_v1` ở cuối file bù lại
phần "đi qua đúng đường HTTP" mà cách gọi thẳng không phủ tới.

Bốn tính chất, mỗi cái là một "chứng minh đỏ" bắt buộc của việc mở tên bảng ba
phần / hai lakehouse — cả ba điều kiện lọc dưới đây đã kiểm trực tiếp trên
`uq_item_active_name`/migration `0003`
(`UNIQUE (workspace_id, type, name) WHERE state = 'active'`):

1. `type='lakehouse'` là điều kiện BẮT BUỘC trong câu truy vấn phân giải — bỏ
   nó, `test_type_filter_prevents_a_same_named_sql_script_from_shadowing_the_
   lakehouse` phải ĐỎ: `type` nằm TRONG ràng buộc duy nhất, nên một
   `sql_script` và một `lakehouse` cùng tên cùng tồn tại được trong một
   workspace, và thiếu điều kiện lọc thì phân giải có thể trả về item SAI KIỂU.
2. `state='active'` là điều kiện BẮT BUỘC — bỏ nó,
   `test_a_soft_deleted_lakehouse_does_not_shadow_the_one_recreated_with_the_
   same_name` phải ĐỎ: ràng buộc chỉ PARTIAL trên `active`, nên một lakehouse
   đã xoá mềm và một lakehouse MỚI cùng tên cùng tồn tại được.
3. `workspace_id` giới hạn phạm vi — bỏ nó,
   `test_resolution_is_scoped_to_the_requested_workspace` phải ĐỎ: tên
   lakehouse chỉ duy nhất TRONG một workspace, không phải toàn hệ thống.
4. MỘT round trip cho toàn bộ danh sách tên — đếm bằng `sql_log`, không phải
   một biến đếm tự viết trong `resolve_lakehouses` (không có biến đó — chính
   vì vậy phải đếm câu lệnh THẬT).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import ACTIVE, DEFAULT_TENANT_ID, DELETED, Item
from loom_api.routers.internal import resolve_lakehouses
from loom_core.item_definitions import ItemType
from loom_core.schemas import LakehouseResolveRequest

from .conftest import ApiWorld, RbacFixture

pytestmark = pytest.mark.integration


async def _add_item(
    f: RbacFixture,
    workspace_id: uuid.UUID,
    name: str,
    item_type: ItemType,
    state: str = ACTIVE,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    f.session.add(
        Item(
            id=item_id,
            tenant_id=DEFAULT_TENANT_ID,
            workspace_id=workspace_id,
            type=str(item_type),
            name=name,
            display_name=name,
            definition={"schema_version": 1},
            definition_hash="x" * 64,
            state=state,
            created_by=f.user_alice,
            updated_by=f.user_alice,
        )
    )
    await f.session.flush()
    return item_id


async def test_resolves_a_lakehouse_name_to_its_id(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    lakehouse_id = await _add_item(f, f.ws_a, "sales-lake", ItemType.lakehouse)

    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=["sales-lake"]), session=f.session
    )

    assert result.ids == {"sales-lake": lakehouse_id}


async def test_unknown_name_resolves_to_none_alongside_a_known_one(
    rbac_fixture: RbacFixture,
) -> None:
    f = rbac_fixture
    known_id = await _add_item(f, f.ws_a, "sales-lake", ItemType.lakehouse)

    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=["sales-lake", "khong-ton-tai"]),
        session=f.session,
    )

    assert result.ids == {"sales-lake": known_id, "khong-ton-tai": None}


async def test_type_filter_prevents_a_same_named_sql_script_from_shadowing_the_lakehouse(
    rbac_fixture: RbacFixture,
) -> None:
    """`uq_item_active_name` cho phép một `sql_script` và một `lakehouse` cùng
    tên `sales` cùng active trong CÙNG workspace (khoá là `(workspace_id, type,
    name)`, không chỉ `(workspace_id, name)`) — phân giải PHẢI thấy đúng cái
    `lakehouse`, không phải cái kia hay một kết quả không xác định."""
    f = rbac_fixture
    # Lakehouse trước, sql_script SAU: nếu bản cài bỏ lọc `type`, câu truy vấn
    # không `ORDER BY` gì và Postgres trả một seq scan nhỏ theo thứ tự vật lý
    # (== thứ tự chèn) — hàng chèn SAU (`sql_script`) sẽ "thắng" trong dict
    # `found` (key trùng, gán sau đè gán trước), khiến kết quả SAI hẳn thay vì
    # tình cờ đúng. Thứ tự chèn không quan trọng khi lọc `type` CÓ mặt: nó chỉ
    # còn đúng MỘT hàng khớp, bất kể chèn trước hay sau.
    lakehouse_id = await _add_item(f, f.ws_a, "sales", ItemType.lakehouse)
    await _add_item(f, f.ws_a, "sales", ItemType.sql_script)

    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=["sales"]), session=f.session
    )

    assert result.ids == {"sales": lakehouse_id}


async def test_a_soft_deleted_lakehouse_does_not_shadow_the_one_recreated_with_the_same_name(
    rbac_fixture: RbacFixture,
) -> None:
    """Ràng buộc unique chỉ PARTIAL trên `state = 'active'`, nên một hàng
    `deleted` và một hàng `active` CÙNG tên cùng tồn tại được — phân giải phải
    thấy bản ĐANG SỐNG, không phải bản đã xoá.

    Chèn bản ACTIVE TRƯỚC, bản DELETED SAU — cùng lý do đã ghi ở phép kiểm lọc
    `type` phía trên: nếu bản cài bỏ lọc `state`, câu truy vấn không `ORDER BY`
    và Postgres trả một seq scan nhỏ theo thứ tự chèn, nên hàng chèn SAU
    (`deleted`) sẽ "thắng" trong dict `found` — thứ tự này khiến phép kiểm ĐỎ
    một cách tất định khi lọc bị bỏ, thay vì tình cờ vẫn đúng."""
    f = rbac_fixture
    active_id = await _add_item(f, f.ws_a, "sales", ItemType.lakehouse)
    await _add_item(f, f.ws_a, "sales", ItemType.lakehouse, state=DELETED)

    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=["sales"]), session=f.session
    )

    assert result.ids == {"sales": active_id}


async def test_resolution_is_scoped_to_the_requested_workspace(rbac_fixture: RbacFixture) -> None:
    """Cùng tên `sales`, nhưng item nằm ở `ws_b` — hỏi trong `ws_a` phải ra
    `None`, không phải id của item ở workspace khác."""
    f = rbac_fixture
    await _add_item(f, f.ws_b, "sales", ItemType.lakehouse)

    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=["sales"]), session=f.session
    )

    assert result.ids == {"sales": None}


async def test_one_query_resolves_the_whole_batch(rbac_fixture: RbacFixture) -> None:
    f = rbac_fixture
    expected = {
        name: await _add_item(f, f.ws_a, name, ItemType.lakehouse) for name in ("a", "b", "c")
    }

    mark = len(f.sql_log)
    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=list(expected)), session=f.session
    )
    statements = f.statements_since(mark)

    assert len(statements) == 1, (
        f"lô {len(expected)} tên phải là MỘT round trip, thấy {len(statements)}: {statements}"
    )
    assert result.ids == expected


async def test_empty_name_list_resolves_to_empty_without_querying(
    rbac_fixture: RbacFixture,
) -> None:
    f = rbac_fixture
    mark = len(f.sql_log)

    result = await resolve_lakehouses(
        LakehouseResolveRequest(workspace_id=f.ws_a, names=[]), session=f.session
    )

    assert result.ids == {}
    assert f.statements_since(mark) == []


# --------------------------------------------------- qua đúng đường HTTP thật


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


async def test_resolves_over_http(api_world: ApiWorld) -> None:
    """Không có `PrincipalDep`/cookie phiên nào áp dụng ở đây — cùng lý do
    `/internal/authz/items` không đọc cookie (xem docstring `routers/
    internal.py`): người gọi là `loom-query`, không phải trình duyệt."""
    lakehouse_id = await _insert_lakehouse(api_world, api_world.ws_a, "sales-lake")

    r = await api_world.client.post(
        "/internal/lakehouses/resolve",
        json={"workspace_id": str(api_world.ws_a), "names": ["sales-lake", "khong-ton-tai"]},
    )

    assert r.status_code == 200
    assert r.json() == {"ids": {"sales-lake": str(lakehouse_id), "khong-ton-tai": None}}


async def test_the_endpoint_is_not_reachable_under_api_v1(api_world: ApiWorld) -> None:
    """Chốt chống-hồi-quy nhỏ, cùng `test_internal_authz_api.py`: nếu ai đó lỡ
    gắn router `internal` dưới `/api/v1`, request qua đường cũ vẫn phải 404 —
    router chỉ sống ở `/internal` (xem `test_internal_route_boundary.py` cho
    phép canh ở tầng cấu trúc route)."""
    r = await api_world.client.post(
        "/api/v1/internal/lakehouses/resolve",
        json={"workspace_id": str(uuid.uuid4()), "names": []},
    )
    assert r.status_code == 404
