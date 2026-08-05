"""Audit phải chia chung số phận với thao tác nó ghi lại.

Một thay đổi không dấu vết là đúng thứ audit tồn tại để ngăn; một dấu vết nói về
thay đổi chưa xảy ra thì tệ hơn không có audit, vì nó làm người đọc tin vào một
thứ sai. Cả hai chỉ tránh được nếu audit nằm trong CÙNG transaction.
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.audit import AuditReader
from loom_api.item_store import ItemStore
from loom_api.models import DEFAULT_TENANT_ID, AuditLog, Item
from loom_api.permissions import Forbidden, NotVisible
from loom_core.item_definitions import ItemType
from loom_core.roles import Role

pytestmark = pytest.mark.integration


async def _count_audit(session, resource_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.resource_id == resource_id)
        )
    ).scalar_one()


async def test_create_writes_one_audit_row_carrying_the_request_id(rbac_fixture):
    """`request_id` là thứ nối một dòng audit với log và trace của cùng request.
    Không có nó thì từ audit không có đường nào tới log của đúng pod."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="req-abc")

    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="co-audit",
        display_name="Có audit",
        definition={"schema_version": 1, "sql": ""},
    )

    rows = list(
        (
            await f.session.execute(
                select(AuditLog).where(AuditLog.resource_id == item.id)
            )
        ).scalars().all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "item.create"
    assert row.request_id == "req-abc"
    assert row.workspace_id == f.ws_a
    assert row.actor_user_id == f.user_bob
    assert row.resource_type == "item"


async def test_summary_names_the_changed_fields_and_omits_the_definition(rbac_fixture):
    """`item_version` đã giữ definition đầy đủ, và với item `connection` thì
    definition mang `secret_ref`.

    Khẳng định CẢ HAI chiều: không có khoá `definition`, VÀ có đúng các khoá mong
    đợi. Chỉ khẳng định "không có definition" thì một summary RỖNG cũng pass.
    """
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r")
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="doi-noi-dung",
        display_name="Đổi nội dung",
        definition={"schema_version": 1, "sql": "SELECT 1"},
    )

    await store.update(
        item.id,
        expected_version=1,
        definition={"schema_version": 1, "sql": "SELECT 2"},
    )

    row = (
        await f.session.execute(
            select(AuditLog).where(
                AuditLog.resource_id == item.id, AuditLog.action == "item.update"
            )
        )
    ).scalars().one()

    assert "definition" not in row.summary
    assert set(row.summary) == {"changed", "version"}
    assert row.summary["changed"] == ["definition"]
    assert row.summary["version"] == 2


async def test_a_rename_is_recorded_as_a_rename(rbac_fixture):
    """Đổi tên MỘT MÌNH phải hiện trong `changed` — đây là thay đổi mà
    `definition_hash` không thấy, và là lý do ETag là `version`."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r")
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="doi-ten",
        display_name="Tên cũ",
        definition={"schema_version": 1, "sql": ""},
    )

    await store.update(item.id, expected_version=1, display_name="Tên mới")

    row = (
        await f.session.execute(
            select(AuditLog).where(
                AuditLog.resource_id == item.id, AuditLog.action == "item.update"
            )
        )
    ).scalars().one()
    assert row.summary["changed"] == ["display_name"]


async def test_a_noop_update_writes_no_audit_row(rbac_fixture):
    """Không đổi gì thì không có gì để ghi. Ghi vẫn thì dấu vết đầy tiếng ồn và
    người đọc bắt đầu bỏ qua nó — mất tác dụng theo cách tệ nhất."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r")
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="khong-doi",
        display_name="Không đổi",
        definition={"schema_version": 1, "sql": "SELECT 1"},
    )
    before = await _count_audit(f.session, item.id)

    await store.update(
        item.id,
        expected_version=1,
        definition={"schema_version": 1, "sql": "SELECT 1"},
        display_name="Không đổi",
    )

    assert await _count_audit(f.session, item.id) == before


async def test_delete_and_restore_are_recorded(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r")
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="xoa-roi-phuc",
        display_name="Xoá rồi phục",
        definition={"schema_version": 1, "sql": ""},
    )
    await store.update(item.id, expected_version=1, display_name="Đã đổi")
    await store.restore_version(item.id, version=1)
    await store.soft_delete(item.id)

    actions = [
        r.action
        for r in (
            await f.session.execute(
                select(AuditLog)
                .where(AuditLog.resource_id == item.id)
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).scalars().all()
    ]
    assert actions.count("item.create") == 1
    assert actions.count("item.update") == 1
    assert actions.count("item.restore") == 1
    assert actions.count("item.delete") == 1


async def test_a_failure_before_commit_loses_both_the_item_and_its_audit_row(
    db_engine, committed_workspace
):
    """Ép lỗi SAU khi ghi thay đổi và TRƯỚC commit, rồi khẳng định cả hai đều mất.

    Đây là phép kiểm duy nhất phân biệt "audit trong cùng transaction" với "audit
    ghi riêng": với thiết kế sai, item biến mất nhưng dòng audit sống sót — hoặc
    ngược lại. Cần một session tự quản (không phải fixture rollback sẵn) mới thấy
    được, nên test này dựng session riêng và kiểm bằng một session thứ hai.
    """
    ws_id, principal = committed_workspace
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    with pytest.raises(RuntimeError):
        async with maker() as session:
            store = ItemStore(session, principal, request_id="r-hong")
            await store.create(
                workspace_id=ws_id,
                item_type=ItemType.sql_script,
                name="se-mat",
                display_name="Sẽ mất",
                definition={"schema_version": 1, "sql": ""},
            )
            raise RuntimeError("hỏng trước commit")

    async with maker() as session:
        items = (
            await session.execute(
                select(func.count()).select_from(Item).where(Item.name == "se-mat")
            )
        ).scalar_one()
        audits = (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.request_id == "r-hong")
            )
        ).scalar_one()

    assert items == 0, "item còn lại dù transaction hỏng"
    assert audits == 0, "dòng audit còn lại dù transaction hỏng"


async def test_contributor_cannot_read_audit(rbac_fixture):
    """`audit_read` chỉ có trong ACTION_MATRIX từ member lên. Contributor sửa được
    item nhưng không cần biết ai khác đã sửa gì."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    reader = AuditReader(f.session, f.principal_bob)
    with pytest.raises(Forbidden):
        await reader.list_for_workspace(f.ws_a, limit=10)


async def test_member_can_read_audit_and_sees_only_this_workspace(rbac_fixture):
    """Cùng lúc canh hai thứ: member đọc được, VÀ bộ lọc workspace có tác dụng —
    một reader bỏ mệnh đề `workspace_id` vẫn pass nếu chỉ kiểm 'đọc được'."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_b), role=Role.member)

    store = ItemStore(f.session, f.principal_bob, request_id="r")
    in_a = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="trong-a",
        display_name="Trong A",
        definition={"schema_version": 1, "sql": ""},
    )
    await store.create(
        workspace_id=f.ws_b,
        item_type=ItemType.sql_script,
        name="trong-b",
        display_name="Trong B",
        definition={"schema_version": 1, "sql": ""},
    )
    await f.session.flush()

    reader = AuditReader(f.session, f.principal_bob)
    page = await reader.list_for_workspace(f.ws_a, limit=50)
    assert {r.resource_id for r in page.items} == {in_a.id}


async def test_stranger_reading_audit_gets_404_not_403(rbac_fixture):
    """Không có vai trò nào → 404: với người đó workspace không tồn tại. Trả 403
    là xác nhận nó có thật."""
    f = rbac_fixture
    reader = AuditReader(f.session, f.principal_bob)
    with pytest.raises(NotVisible):
        await reader.list_for_workspace(f.ws_a, limit=10)


async def test_audit_paging_never_skips_or_repeats_within_one_transaction(rbac_fixture):
    """Mọi dòng audit của cùng một transaction chia nhau một `created_at`, vì
    `now()` của Postgres là thời điểm bắt đầu transaction. Nên khoá sắp xếp phải là
    `(created_at, id)`; thiếu `id` thì lật trang nhảy hoặc lặp bản ghi."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.member)
    store = ItemStore(f.session, f.principal_bob, request_id="r")
    for i in range(9):
        await store.create(
            workspace_id=f.ws_a,
            item_type=ItemType.sql_script,
            name=f"nhieu-{i}",
            display_name=f"N{i}",
            definition={"schema_version": 1, "sql": ""},
        )
    await f.session.flush()

    reader = AuditReader(f.session, f.principal_bob)
    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(10):
        page = await reader.list_for_workspace(f.ws_a, limit=2, cursor=cursor)
        seen.extend(r.id for r in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None, "không lật hết được trong 10 vòng"
    assert len(seen) == 9, f"mong 9 dòng, nhận {len(seen)}"
    assert len(set(seen)) == 9, "có bản ghi lặp giữa các trang"
