"""Hai PATCH song song trên cùng một item — đúng MỘT phải nhận 412.

Kiểm ETag tuần tự là kiểm giả. Chạy hai lần `update` nối đuôi nhau trên một
session thì lần thứ hai đọc version đã bị lần thứ nhất bump, nên phép so version
khớp dù hàng có bị khoá hay không. Cái mà `FOR UPDATE` ngăn chỉ tồn tại khi hai
transaction đọc CÙNG một version trước khi bên nào ghi — và chỉ một test thật sự
đồng thời mới dựng được tình huống đó.

Bốn tiền đề được KHẲNG ĐỊNH chứ không giả định, vì thiếu bất kỳ cái nào thì test
vẫn xanh mà không kiểm tra gì (bài học của `test_role_store_concurrent.py`):

1. Dữ liệu đã COMMIT — `db_session` bọc mọi thứ trong một transaction bị
   rollback, nên hai session khác sẽ không thấy item nào và cả hai "412" vì lý
   do sai. Fixture ở đây commit thật và tự dọn.
2. Hai coroutine dùng hai CONNECTION khác nhau — kiểm bằng `pg_backend_pid()`,
   không phải bằng việc nhìn code.
3. Hai coroutine thật sự CHỒNG LẤN. `asyncio.Barrier` chặn cả hai lại tới khi
   cả hai đã mở connection VÀ đã làm nóng cache quyền. Không làm nóng thì câu
   lệnh đầu tiên `update` gửi đi là câu hỏi phân quyền, hai bên lệch pha một
   round trip, và cửa sổ đua chỉ mở theo xác suất — Task 13 đo được 6/10.
   Làm nóng xong thì câu lệnh đầu tiên sau hàng rào là chính `SELECT ... FOR
   UPDATE`, và cả hai gửi nó đi trước khi bên nào nhận được trả lời.
4. Ở biến thể `preload`, đối tượng đã đọc vẫn CÒN trong identity map khi ghi.
   Identity map giữ tham chiếu yếu, nên `await store.get(...)` mà bỏ giá trị đi
   là không giữ được gì cả — đo được: bỏ `populate_existing` mà test vẫn xanh
   10/10 lần. Tiền đề này bị bỏ sót đúng một vòng và đó là lý do nó nằm đây.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from loom_api.item_store import ItemStore, VersionMismatch
from loom_api.models import (
    DEFAULT_TENANT_ID,
    AppUser,
    AuditLog,
    Item,
    ItemVersion,
    RoleAssignment,
    Workspace,
)
from loom_core.item_definitions import ItemType
from loom_core.roles import Role
from loom_core.schemas import Principal

pytestmark = pytest.mark.integration


@pytest.fixture
async def committed_item(
    db_engine: AsyncEngine,
) -> AsyncIterator[tuple[uuid.UUID, Principal]]:
    """Một item version 1 đã COMMIT, cùng một contributor có quyền sửa nó."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    bob = uuid.uuid4()
    ws_id = uuid.uuid4()
    tag = ws_id.hex[:8]
    principal = Principal(
        user_id=bob,
        subject=f"bob-{tag}",
        email=f"bob-{tag}@loom.local",
        display_name="bob",
    )

    async with maker() as session:
        session.add(
            AppUser(
                id=bob,
                tenant_id=DEFAULT_TENANT_ID,
                subject=f"bob-{tag}",
                email=f"bob-{tag}@loom.local",
                display_name="bob",
            )
        )
        await session.flush()
        session.add(
            Workspace(
                id=ws_id,
                tenant_id=DEFAULT_TENANT_ID,
                domain_id=None,
                name=f"ws-dua-{tag}",
                display_name=f"ws-dua-{tag}",
                storage_prefix=f"ws-dua-{tag}",
                created_by=bob,
                updated_by=bob,
            )
        )
        await session.flush()
        session.add(
            RoleAssignment(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                principal_type="user",
                principal_user_id=bob,
                principal_group=None,
                scope_type="workspace",
                scope_id=ws_id,
                role=str(Role.contributor),
                created_by=bob,
            )
        )
        await session.flush()
        # Tạo QUA store để hàng `item_version` số 1 có thật — phép đếm ở cuối
        # test dựa vào nó.
        item = await ItemStore(session, principal, request_id="fixture").create(
            workspace_id=ws_id,
            item_type=ItemType.sql_script,
            name="dua",
            display_name="Đua",
            definition={"schema_version": 1, "sql": "V1"},
        )
        item_id = item.id
        await session.commit()

    try:
        yield item_id, principal
    finally:
        async with maker() as session:
            # audit_log.actor_user_id có FK tới app_user, nên phải xoá audit
            # TRƯỚC user. Task 19 làm update() ghi audit, và thiếu dòng này thì
            # teardown vỡ vì FK — fixture im lặng để lại workspace, và những
            # test gán vai trò cấp tenant (thấy MỌI workspace) bắt đầu đỏ ở chỗ
            # không liên quan gì tới chúng.
            await session.execute(delete(AuditLog).where(AuditLog.workspace_id == ws_id))
            await session.execute(delete(ItemVersion).where(ItemVersion.item_id == item_id))
            await session.execute(delete(Item).where(Item.id == item_id))
            await session.execute(delete(RoleAssignment).where(RoleAssignment.scope_id == ws_id))
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.execute(delete(AppUser).where(AppUser.id == bob))
            await session.commit()


async def _race(
    db_engine: AsyncEngine,
    item_id: uuid.UUID,
    principal: Principal,
    *,
    preload: bool,
) -> tuple[list[str], list[int]]:
    """Hai `update(expected_version=1)` song song. Trả về (kết quả, backend pid).

    `preload=True` cho mỗi session đọc item TRƯỚC hàng rào — đúng hình dạng của
    một handler đọc rồi mới ghi.
    """
    barrier = asyncio.Barrier(2)
    pids: list[int] = []

    async def patch(sql: str) -> str:
        # MỖI coroutine một session riêng = một transaction riêng. Dùng chung
        # một session thì không có đồng thời nào cả, chỉ có hai lệnh nối đuôi.
        maker = async_sessionmaker(db_engine, expire_on_commit=False)
        async with maker() as session:
            # Mở connection TRƯỚC hàng rào: để việc bắt tay TCP xảy ra sau đó thì
            # hai bên lệch pha vì lý do không liên quan gì tới transaction.
            pids.append((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            store = ItemStore(session, principal, request_id="r")
            preloaded: Item | None = None
            if preload:
                # GÁN vào biến cục bộ, không gọi rồi bỏ giá trị đi. Identity map
                # của SQLAlchemy giữ tham chiếu YẾU: không ai giữ đối tượng thì
                # nó bị thu gom ngay, map rỗng khi qua hàng rào, và
                # `_lock_active` nạp một đối tượng mới tinh — nhánh bản-chụp-cũ
                # mà test này đặt tên KHÔNG BAO GIỜ xảy ra. Đo được: bỏ
                # `populate_existing` khỏi `_lock_active` mà test vẫn xanh 10/10.
                preloaded = await store.get(item_id)
            else:
                # Dùng thuộc tính riêng có chủ ý: cần đúng thực thể
                # `PermissionService` mà `update` sẽ dùng lại, chứ không phải
                # một cái khác cũng trả về cùng kết quả.
                await store._perms.effective_role_for_item(item_id)
            await barrier.wait()
            if preloaded is not None:
                # Tiền đề 4, khẳng định chứ không giả định như ba tiền đề kia:
                # đối tượng đã đọc vẫn còn TRONG identity map ở đúng thời điểm
                # `update` chạy. Không có dòng này thì việc giữ tham chiếu là
                # một chi tiết không ai bảo vệ, và lần dọn dẹp sau sẽ lặng lẽ
                # trả test về chỗ nó không kiểm gì.
                assert any(st.obj() is preloaded for st in session.identity_map.all_states()), (
                    "đối tượng đã đọc đã rời identity map — không còn bản chụp cũ nào để kiểm"
                )
            try:
                await store.update(
                    item_id, expected_version=1, definition={"schema_version": 1, "sql": sql}
                )
                await session.commit()
                return "ok"
            except VersionMismatch:
                await session.rollback()
                return "412"
            except IntegrityError:
                # Không phải kết quả hợp lệ — nhưng bắt lại để thông báo lỗi nói
                # được ĐIỀU GÌ đã xảy ra. Khi phép so version chạy trên dữ liệu
                # cũ, cả hai bên cùng tính ra version 2 và bên thứ hai vỡ
                # `uq_item_version`: một 500, không phải một 412.
                await session.rollback()
                return "vo-uq_item_version"

    results = await asyncio.gather(patch("A"), patch("B"))
    return results, pids


async def _final_state(
    db_engine: AsyncEngine, item_id: uuid.UUID
) -> tuple[int, int, str, str | None]:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        version, definition = (
            await session.execute(select(Item.version, Item.definition).where(Item.id == item_id))
        ).one()
        rows = (
            await session.execute(
                select(func.count()).select_from(ItemVersion).where(ItemVersion.item_id == item_id)
            )
        ).scalar_one()
        top = (
            await session.execute(
                select(ItemVersion.definition)
                .where(ItemVersion.item_id == item_id, ItemVersion.version == version)
                .limit(1)
            )
        ).scalar_one_or_none()
    return version, rows, definition["sql"], None if top is None else top["sql"]


async def _assert_exactly_one_winner(
    db_engine: AsyncEngine, item_id: uuid.UUID, results: list[str], pids: list[int]
) -> None:
    # Tiền đề 2: hai backend Postgres KHÁC nhau đã tham gia.
    assert len(set(pids)) == 2, f"hai coroutine dùng chung một connection: {pids}"
    assert sorted(results) == ["412", "ok"], f"results={results}"

    version, rows, item_sql, version_sql = await _final_state(db_engine, item_id)
    assert version == 2, f"version={version} results={results}"
    assert rows == 2, f"số hàng item_version={rows} — phải là v1 + đúng một v2"
    # Nội dung trên `item` và nội dung của hàng version cùng số PHẢI khớp. Đây là
    # thứ mà một lost update làm hỏng và phép đếm không nhìn thấy: bên thua ghi
    # đè `item` trong khi hàng version 2 vẫn mang nội dung của bên thắng, và từ
    # đó lịch sử nói dối về chính hàng nó mô tả.
    assert item_sql == version_sql, f"item.sql={item_sql!r} nhưng version 2 nói {version_sql!r}"


async def test_exactly_one_of_two_concurrent_patches_wins(
    db_engine: AsyncEngine, committed_item
) -> None:
    item_id, principal = committed_item
    results, pids = await _race(db_engine, item_id, principal, preload=False)
    await _assert_exactly_one_winner(db_engine, item_id, results, pids)


async def test_a_session_that_already_read_the_item_still_sees_the_fresh_version(
    db_engine: AsyncEngine, committed_item
) -> None:
    """Khoá hàng rồi so version với một BẢN CHỤP CŨ thì cũng không phải phép kiểm.

    Mặc định của SQLAlchemy: một truy vấn trả về hàng của đối tượng đã có trong
    identity map sẽ trả lại chính đối tượng đó và KHÔNG ghi đè thuộc tính đã nạp.
    Nên với một session đã `get()` item (đường GET-rồi-PATCH bình thường của một
    API), `SELECT ... FOR UPDATE` chờ đúng chỗ, nhận về hàng version 2 — rồi
    `item.version` trong bộ nhớ vẫn là 1 và phép so vẫn khớp. `populate_existing`
    là thứ đóng khoảng đó lại; test trên không nhìn thấy nó vì ở đó item chưa
    từng được nạp.
    """
    item_id, principal = committed_item
    results, pids = await _race(db_engine, item_id, principal, preload=True)
    await _assert_exactly_one_winner(db_engine, item_id, results, pids)
