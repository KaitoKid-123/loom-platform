"""Quy tắc admin-cuối-cùng dưới hai transaction song song.

Kiểm tuần tự không chứng minh được gì ở đây: cả hai lần thu đều đọc "còn hai
admin" trước khi lần nào commit, rồi cả hai xoá — workspace mất admin. Đó là
đúng cái mà một test tuần tự KHÔNG NHÌN THẤY, vì nó chỉ có một transaction.

Ba tiền đề của test này được KHẲNG ĐỊNH chứ không giả định, vì thiếu bất kỳ cái
nào thì test vẫn xanh mà không kiểm tra gì:

1. Dữ liệu đã COMMIT — một session thứ ba đọc thấy hai admin trước khi chạy.
   Nếu fixture giữ dữ liệu trong một transaction chưa commit thì hai coroutine
   nhìn thấy không admin nào và cả hai "thành công" một cách vô nghĩa.
2. Hai coroutine dùng hai CONNECTION khác nhau — kiểm bằng `pg_backend_pid()`,
   không phải bằng việc nhìn code. Dùng chung một session thì không có đồng
   thời nào cả, chỉ có hai lệnh nối đuôi.
3. Hai coroutine thật sự chồng lấn — một `asyncio.Barrier` chặn cả hai lại tới
   khi cả hai đã mở connection xong, nên không có chuyện lần thứ nhất chạy hết
   rồi lần thứ hai mới bắt đầu.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, AppUser, RoleAssignment, Workspace
from loom_api.role_store import LastAdminError, RoleStore
from loom_core.roles import Role
from loom_core.schemas import Principal

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_two_admins(
    db_engine: AsyncEngine,
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, Principal]]:
    """Một workspace với hai admin, đã COMMIT thật.

    Không dùng `db_session` được: fixture đó bọc mọi thứ trong một transaction
    bị rollback, nên hai session song song sẽ không thấy gì cả — và test sẽ
    xanh vì lý do sai.
    """
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    ws_id = uuid.uuid4()
    tag = ws_id.hex[:8]

    async with maker() as session:
        session.add_all(
            [
                AppUser(
                    id=uid,
                    tenant_id=DEFAULT_TENANT_ID,
                    subject=f"{name}-{tag}",
                    email=f"{name}-{tag}@loom.local",
                    display_name=name,
                )
                for uid, name in ((alice, "alice"), (bob, "bob"))
            ]
        )
        await session.flush()
        session.add(
            Workspace(
                id=ws_id,
                tenant_id=DEFAULT_TENANT_ID,
                domain_id=None,
                name=f"ws-conc-{tag}",
                display_name=f"ws-conc-{tag}",
                storage_prefix=f"ws-conc-{tag}",
                created_by=alice,
                updated_by=alice,
            )
        )
        await session.flush()
        session.add_all(
            [
                RoleAssignment(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    principal_type="user",
                    principal_user_id=uid,
                    principal_group=None,
                    scope_type="workspace",
                    scope_id=ws_id,
                    role=str(Role.admin),
                    created_by=alice,
                )
                for uid in (alice, bob)
            ]
        )
        await session.commit()

    principal_alice = Principal(
        user_id=alice,
        subject=f"alice-{tag}",
        email=f"alice-{tag}@loom.local",
        display_name="alice",
    )
    try:
        yield ws_id, alice, bob, principal_alice
    finally:
        async with maker() as session:
            await session.execute(delete(RoleAssignment).where(RoleAssignment.scope_id == ws_id))
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.execute(delete(AppUser).where(AppUser.id.in_([alice, bob])))
            await session.commit()


async def _admins_at(db_engine: AsyncEngine, ws_id: uuid.UUID) -> list[uuid.UUID]:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        return list(
            (
                await session.execute(
                    select(RoleAssignment.principal_user_id).where(
                        RoleAssignment.scope_type == "workspace",
                        RoleAssignment.scope_id == ws_id,
                        RoleAssignment.role == str(Role.admin),
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_two_concurrent_revokes_cannot_both_succeed(
    db_engine: AsyncEngine, seeded_two_admins
) -> None:
    ws_id, alice, bob, principal_alice = seeded_two_admins

    # Tiền đề 1: dữ liệu ĐÃ commit và một connection khác đọc thấy.
    assert sorted(await _admins_at(db_engine, ws_id)) == sorted([alice, bob])

    barrier = asyncio.Barrier(2)
    pids: list[int] = []

    async def revoke(target: uuid.UUID) -> str:
        # MỖI coroutine một session riêng = một transaction riêng. Dùng chung
        # một session thì không có đồng thời nào cả.
        maker = async_sessionmaker(db_engine, expire_on_commit=False)
        async with maker() as session:
            # Mở connection TRƯỚC hàng rào: nếu để việc bắt tay TCP/TLS xảy ra
            # sau đó thì hai bên lệch pha vì lý do không liên quan gì tới
            # transaction, và cửa sổ đua hẹp lại một cách tình cờ.
            pids.append((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            store = RoleStore(session, principal_alice)
            # Làm nóng cache quyền TRƯỚC hàng rào. Không có dòng này thì lệnh
            # đầu tiên `revoke` gửi đi là câu hỏi phân quyền, và hai coroutine
            # lệch pha một round trip — đo được: cửa sổ đua chỉ mở khoảng 6/10
            # lần, tức là test vẫn XANH 4/10 lần khi bug có mặt. Làm nóng xong
            # thì câu lệnh đầu tiên sau hàng rào là chính SELECT đếm admin, và
            # cả hai gửi nó đi trước khi bên nào nhận được trả lời.
            #
            # Dùng thuộc tính riêng có chủ ý: cần đúng thực thể
            # `PermissionService` mà `revoke` sẽ dùng lại, chứ không phải một
            # cái khác cũng trả về cùng kết quả.
            await store._perms.effective_role_for_workspace(ws_id)
            await barrier.wait()
            try:
                await store.revoke(scope=("workspace", ws_id), user_id=target)
                await session.commit()
                return "ok"
            except LastAdminError:
                await session.rollback()
                return "chan"

    results = await asyncio.gather(revoke(alice), revoke(bob))

    # Tiền đề 2: hai backend Postgres KHÁC nhau đã tham gia.
    assert len(set(pids)) == 2, f"hai coroutine dùng chung một connection: {pids}"

    left = await _admins_at(db_engine, ws_id)
    assert len(left) >= 1, f"mất hết admin — results={results}"
    assert "chan" in results, f"cả hai đều qua được — results={results}"
    # Chính xác hơn hai câu trên gộp lại: đúng một lệnh qua, đúng một admin còn.
    assert sorted(results) == ["chan", "ok"], f"results={results}"
    assert len(left) == 1, f"left={left} results={results}"
