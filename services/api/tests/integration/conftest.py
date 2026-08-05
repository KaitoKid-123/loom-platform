"""Fixture dùng chung cho integration test.

**Một** container Postgres cho cả session, và nó là container đã `alembic
upgrade head` chứ không phải `Base.metadata.create_all`. Ba lý do, theo thứ tự
quan trọng:

1. Thứ chạy trên Aiven là MIGRATION. Một test quyền chạy trên schema dựng từ
   metadata chứng minh "model này hành xử như vậy", không phải "database thật
   hành xử như vậy" — đúng cái khoảng cách mà Task 8 đã phải bịt.
2. Migration 0001 seed hàng `tenant`. `create_all` không seed gì, nên mọi FK
   `tenant_id` sẽ hỏng và fixture phải tự chèn một hàng tenant — một khác biệt
   nữa giữa test và thật.
3. `test_migrations.py` đã dựng đúng container đó rồi. Dựng cái thứ hai là ba
   giây initdb cho mỗi lần chạy, đổi lấy không gì cả.

`test_user_store.py` CỐ Ý vẫn giữ container riêng: fixture của nó `DELETE` rồi
`COMMIT` trên `app_user`/`user_session`, nên nó không chia sẻ được với các test
chỉ rollback mà không ràng buộc thứ tự chạy giữa các file.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer

from loom_api.db import Database
from loom_api.item_store import ItemStore
from loom_api.main import create_app
from loom_api.models import (
    DEFAULT_TENANT_ID,
    AppUser,
    AuditLog,
    Domain,
    Item,
    RoleAssignment,
    Workspace,
)
from loom_core.item_definitions import ItemType
from loom_core.roles import Role
from loom_core.schemas import Principal

from .pg_support import POSTGRES_IMAGE, async_url, run_alembic


@pytest.fixture(scope="session")
def migrated_pg() -> Iterator[PostgresContainer]:
    """Một container cho cả session, đã `alembic upgrade head`.

    Fixture ĐỒNG BỘ có chủ đích: pytest-asyncio mặc định cho mỗi test một event
    loop riêng, nên một fixture async phạm vi session sẽ tạo tài nguyên trên
    loop đã đóng của test đầu tiên. Container không cần loop; engine async thì
    dựng theo từng test ở dưới.
    """
    with PostgresContainer(POSTGRES_IMAGE) as pg:
        result = run_alembic(pg, "upgrade", "head")
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        yield pg


@pytest.fixture
async def db_engine(migrated_pg: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(async_url(migrated_pg))
    yield engine
    await engine.dispose()


@pytest.fixture
def sql_log(db_engine: AsyncEngine) -> list[str]:
    """Mọi câu lệnh THẬT SỰ gửi tới Postgres, theo thứ tự.

    Không phải một bộ đếm do chính code bị kiểm tự tăng. `PermissionService`
    có `query_count`, nhưng một câu khẳng định về `query_count` chỉ chứng minh
    rằng dòng `self.query_count += 1` đã chạy — nó mù hoàn toàn với việc
    `_query_item_roles` gửi hai round trip thay vì một, mà "một truy vấn cho
    một tài nguyên" chính là điều Task 9 tuyên bố.
    """
    statements: list[str] = []

    def record(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: Any
    ) -> None:
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    return statements


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Một `AsyncSession` chạy trong một transaction bị ROLLBACK sau mỗi test.

    Session gắn vào một connection đã `begin()` sẵn, và `join_transaction_mode`
    là `create_savepoint` để code bị kiểm gọi `commit()` cũng không thoát ra
    ngoài transaction bọc ngoài. Nhờ vậy schema (migration) được tái sử dụng cho
    cả session mà không test nào để lại dữ liệu cho test sau.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@dataclass
class RbacFixture:
    session: AsyncSession
    sql_log: list[str] = field(repr=False)
    user_alice: uuid.UUID
    user_bob: uuid.UUID
    domain_x: uuid.UUID
    ws_a: uuid.UUID
    ws_b: uuid.UUID
    item_a1: uuid.UUID
    item_a2: uuid.UUID
    item_b1: uuid.UUID
    principal_alice: Principal
    principal_bob: Principal
    principal_nobody: Principal

    async def grant(
        self,
        scope: tuple[str, uuid.UUID],
        role: Role,
        user: uuid.UUID | None = None,
        group: str | None = None,
    ) -> None:
        assert (user is None) != (group is None), "đúng một trong user/group"
        self.session.add(
            RoleAssignment(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                principal_type="user" if user else "group",
                principal_user_id=user,
                principal_group=group,
                scope_type=scope[0],
                scope_id=scope[1],
                role=str(role),
                created_by=self.user_alice,
            )
        )
        # flush ngay, không đợi autoflush: mọi phép đếm câu lệnh ở dưới chụp
        # `len(sql_log)` ngay trước lần gọi cần đo, và một INSERT bị autoflush
        # hoãn tới đúng lúc đó sẽ được tính vào số round trip của phép đo.
        await self.session.flush()

    def statements_since(self, mark: int) -> list[str]:
        return self.sql_log[mark:]

    # Ba hàm dựng dưới đây cho phép một test thêm tài nguyên RIÊNG của nó thay
    # vì phình bộ cố định ở trên. Quan trọng: bộ cố định là tiền đề mà nhiều
    # file test cùng đọc (Task 11 khẳng định trên đúng ba item), nên thêm vào đó
    # là sửa tiền đề của những test mình chưa đọc.

    async def add_domain(self, name: str) -> uuid.UUID:
        domain_id = uuid.uuid4()
        self.session.add(
            Domain(
                id=domain_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=name,
                display_name=name,
                created_by=self.user_alice,
                updated_by=self.user_alice,
            )
        )
        await self.session.flush()
        return domain_id

    async def add_workspace(self, name: str, domain_id: uuid.UUID | None = None) -> uuid.UUID:
        ws_id = uuid.uuid4()
        self.session.add(
            Workspace(
                id=ws_id,
                tenant_id=DEFAULT_TENANT_ID,
                domain_id=domain_id,
                name=name,
                display_name=name,
                storage_prefix=name,
                created_by=self.user_alice,
                updated_by=self.user_alice,
            )
        )
        await self.session.flush()
        return ws_id

    async def add_item(
        self, workspace_id: uuid.UUID, name: str, tenant_id: uuid.UUID = DEFAULT_TENANT_ID
    ) -> uuid.UUID:
        # `tenant_id` mở ra được vì `item.tenant_id` KHÔNG có foreign key (xem
        # models.py) — một hàng mang tenant lệch với workspace của nó là trạng
        # thái mà database cho phép tồn tại, nên nó phải kiểm được.
        item_id = uuid.uuid4()
        self.session.add(
            Item(
                id=item_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                type="sql_script",
                name=name,
                display_name=name,
                definition={"schema_version": 1, "sql": ""},
                definition_hash="x" * 64,
                created_by=self.user_alice,
                updated_by=self.user_alice,
            )
        )
        await self.session.flush()
        return item_id


@pytest.fixture
async def rbac_fixture(db_session: AsyncSession, sql_log: list[str]) -> RbacFixture:
    """Hai user, một domain, hai workspace, ba item. KHÔNG có assignment nào —
    từng test tự gán đúng thứ nó cần, để mỗi test nói rõ tiền đề của mình."""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    for uid, sub in ((alice, "alice"), (bob, "bob")):
        db_session.add(
            AppUser(
                id=uid,
                tenant_id=DEFAULT_TENANT_ID,
                subject=sub,
                email=f"{sub}@loom.local",
                display_name=sub,
            )
        )
    await db_session.flush()

    domain_x = uuid.uuid4()
    db_session.add(
        Domain(
            id=domain_x,
            tenant_id=DEFAULT_TENANT_ID,
            name="domain-x",
            display_name="Domain X",
            created_by=alice,
            updated_by=alice,
        )
    )
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    db_session.add(
        Workspace(
            id=ws_a,
            tenant_id=DEFAULT_TENANT_ID,
            domain_id=domain_x,
            name="ws-a",
            display_name="WS A",
            storage_prefix="ws-a",
            created_by=alice,
            updated_by=alice,
        )
    )
    # ws_b KHÔNG thuộc domain nào — để test được rằng vai trò domain không tràn
    db_session.add(
        Workspace(
            id=ws_b,
            tenant_id=DEFAULT_TENANT_ID,
            domain_id=None,
            name="ws-b",
            display_name="WS B",
            storage_prefix="ws-b",
            created_by=alice,
            updated_by=alice,
        )
    )
    await db_session.flush()

    items: dict[str, uuid.UUID] = {}
    for key, ws, name in (
        ("a1", ws_a, "item-a1"),
        ("a2", ws_a, "item-a2"),
        ("b1", ws_b, "item-b1"),
    ):
        iid = uuid.uuid4()
        items[key] = iid
        db_session.add(
            Item(
                id=iid,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=ws,
                type="sql_script",
                name=name,
                display_name=name,
                definition={"schema_version": 1, "sql": ""},
                definition_hash="x" * 64,
                created_by=alice,
                updated_by=alice,
            )
        )
    await db_session.flush()

    def principal(uid: uuid.UUID, sub: str, groups: tuple[str, ...]) -> Principal:
        return Principal(
            user_id=uid,
            subject=sub,
            email=f"{sub}@loom.local",
            display_name=sub,
            groups=groups,
        )

    return RbacFixture(
        session=db_session,
        sql_log=sql_log,
        user_alice=alice,
        user_bob=bob,
        domain_x=domain_x,
        ws_a=ws_a,
        ws_b=ws_b,
        item_a1=items["a1"],
        item_a2=items["a2"],
        item_b1=items["b1"],
        principal_alice=principal(alice, "alice", ("data-eng",)),
        principal_bob=principal(bob, "bob", ()),
        # KHÔNG có hàng app_user tương ứng: một principal hoàn toàn xa lạ phải
        # ra cùng kết quả với một user có thật mà chưa được gán gì.
        principal_nobody=principal(uuid.uuid4(), "nobody", ()),
    )


@pytest.fixture
async def contributor_bob(rbac_fixture: RbacFixture) -> None:
    """bob là contributor trên `ws_a` — tiền đề của mọi test ghi."""
    await rbac_fixture.grant(
        user=rbac_fixture.user_bob, scope=("workspace", rbac_fixture.ws_a), role=Role.contributor
    )


@pytest.fixture
async def an_item(rbac_fixture: RbacFixture, contributor_bob: None) -> Item:
    """Một item version 1 trong `ws_a`, tạo QUA `ItemStore.create`.

    Không dùng `RbacFixture.add_item`: hàm đó chèn thẳng hàng `item` và KHÔNG
    sinh hàng `item_version` nào. Mọi test dưới đây đếm version, nên một item
    thiếu version 1 sẽ làm chúng đếm lệch một — và lệch theo hướng cho phép một
    bản cài đặt sai vẫn xanh.

    Người tạo là ALICE trong khi mọi test ghi chạy dưới BOB, và điều đó là cố ý.
    Tạo bằng chính bob thì `updated_by` đã bằng bob TỪ TRƯỚC khi có lần sửa nào,
    nên `assert row.updated_by == user_bob` đúng kể cả với một bản cài đặt không
    hề ghi `updated_by` — một phép kiểm không nhìn thấy được thứ nó đặt tên. Hai
    người khác nhau làm hai vế đó phân biệt được.
    """
    await rbac_fixture.grant(
        user=rbac_fixture.user_alice,
        scope=("workspace", rbac_fixture.ws_a),
        role=Role.contributor,
    )
    store = ItemStore(rbac_fixture.session, rbac_fixture.principal_alice, request_id="fixture")
    return await store.create(
        workspace_id=rbac_fixture.ws_a,
        item_type=ItemType.sql_script,
        name="can-sua",
        display_name="Cần sửa",
        definition={"schema_version": 1, "sql": "SELECT 1"},
    )


@pytest.fixture
async def committed_workspace(
    db_engine: AsyncEngine,
) -> AsyncIterator[tuple[uuid.UUID, Principal]]:
    """Một workspace ĐÃ COMMIT, cùng một principal có quyền tạo item trong đó.

    Khác `rbac_fixture`: fixture kia sống trong một transaction bị rollback cuối
    test, nên một session THỨ HAI không thấy dữ liệu của nó. Test nào cần kiểm
    hành vi transaction — audit có mất cùng thao tác không, hai request đồng thời —
    buộc phải có dữ liệu commit thật, và tự dọn sau.
    """
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    async with maker() as session:
        session.add(
            AppUser(
                id=user_id,
                tenant_id=DEFAULT_TENANT_ID,
                subject=f"committed-{user_id.hex[:8]}",
                email="committed@loom.local",
                display_name="committed",
            )
        )
        await session.flush()
        session.add(
            Workspace(
                id=ws_id,
                tenant_id=DEFAULT_TENANT_ID,
                domain_id=None,
                name=f"ws-committed-{ws_id.hex[:6]}",
                display_name="WS committed",
                storage_prefix=f"workspaces/{ws_id}",
                created_by=user_id,
                updated_by=user_id,
            )
        )
        await session.flush()
        session.add(
            RoleAssignment(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                principal_type="user",
                principal_user_id=user_id,
                principal_group=None,
                scope_type="workspace",
                scope_id=ws_id,
                role=str(Role.admin),
                created_by=user_id,
            )
        )
        await session.commit()

    principal = Principal(
        user_id=user_id,
        subject=f"committed-{user_id.hex[:8]}",
        email="committed@loom.local",
        display_name="committed",
        groups=(),
    )
    try:
        yield ws_id, principal
    finally:
        # Dọn tay: không có transaction nào bao ngoài để rollback hộ. Xoá theo thứ
        # tự khoá ngoại — item và audit trước, rồi workspace, rồi user.
        async with maker() as session:
            await session.execute(delete(AuditLog).where(AuditLog.workspace_id == ws_id))
            await session.execute(delete(Item).where(Item.workspace_id == ws_id))
            await session.execute(delete(RoleAssignment).where(RoleAssignment.scope_id == ws_id))
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.execute(delete(AppUser).where(AppUser.id == user_id))
            await session.commit()


@dataclass
class ApiWorld:
    """App thật + một principal đã xác thực, chạy trên schema đã migrate.

    Khác `rbac_fixture`: ở đây request đi qua đúng đường HTTP — dependency, cổng
    quyền, exception handler RFC 9457, header ETag. Một cổng quyền đúng ở tầng
    store vẫn có thể bị bỏ qua ở tầng router, và chỉ test qua HTTP mới thấy.
    """

    client: AsyncClient
    engine: AsyncEngine
    ws_a: uuid.UUID
    ws_b: uuid.UUID
    user_id: uuid.UUID
    principal: Principal
    grant: Callable[..., Awaitable[None]]
    # Một người dùng thứ hai đã COMMIT, và nó tự vào danh sách dọn. `role_assignment
    # .principal_user_id` có khoá ngoại tới `app_user`, nên mọi test về gán quyền
    # cần một người thật để gán cho — và một test tự chèn app_user rồi quên xoá sẽ
    # làm lệnh xoá user của fixture vỡ vì khoá ngoại, đúng lỗi Task 19 đã gặp.
    make_user: Callable[[str], Awaitable[uuid.UUID]]


@pytest.fixture
async def api_world(
    db_engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[ApiWorld]:
    """Dữ liệu COMMIT thật, vì app mở session riêng và không thấy transaction của test.

    Nhóm của principal nhận qua tham số gián tiếp, mặc định là không có nhóm:

        @pytest.mark.parametrize("api_world", [("authors",)], indirect=True)

    Cần nó vì phân quyền theo nhóm chỉ kiểm được khi phiên MANG nhóm, và mọi test
    khác cố ý chạy với một principal không nhóm — thêm nhóm vào mặc định là sửa
    tiền đề của những test mình chưa đọc.
    """
    groups: tuple[str, ...] = getattr(request, "param", ())
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    user_id, ws_a, ws_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    subject = f"api-{user_id.hex[:8]}"

    async with maker() as session:
        session.add(
            AppUser(
                id=user_id,
                tenant_id=DEFAULT_TENANT_ID,
                subject=subject,
                email="api@loom.local",
                display_name="api",
            )
        )
        await session.flush()
        for ws_id, name in ((ws_a, "a"), (ws_b, "b")):
            session.add(
                Workspace(
                    id=ws_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    domain_id=None,
                    name=f"ws-{name}-{ws_id.hex[:6]}",
                    display_name=f"WS {name.upper()}",
                    storage_prefix=f"workspaces/{ws_id}",
                    created_by=user_id,
                    updated_by=user_id,
                )
            )
        await session.commit()

    principal = Principal(
        user_id=user_id,
        subject=subject,
        email="api@loom.local",
        display_name="api",
        groups=groups,
    )

    async def grant(
        scope: tuple[str, uuid.UUID],
        role: Role,
        *,
        user: uuid.UUID | None = None,
        group: str | None = None,
    ) -> None:
        assert user is None or group is None, "đúng một trong user/group"
        async with maker() as session:
            session.add(
                RoleAssignment(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    principal_type="group" if group else "user",
                    principal_user_id=None if group else (user or user_id),
                    principal_group=group,
                    scope_type=scope[0],
                    scope_id=scope[1],
                    role=str(role),
                    created_by=user_id,
                )
            )
            await session.commit()

    extra_users: list[uuid.UUID] = []

    async def make_user(name: str) -> uuid.UUID:
        uid = uuid.uuid4()
        async with maker() as session:
            session.add(
                AppUser(
                    id=uid,
                    tenant_id=DEFAULT_TENANT_ID,
                    subject=f"{name}-{uid.hex[:8]}",
                    email=f"{name}-{uid.hex[:8]}@loom.local",
                    display_name=name,
                )
            )
            await session.commit()
        extra_users.append(uid)
        return uid

    class _Store:
        """Trả về principal cố định. Xác thực OIDC đã có test riêng; ở đây ta kiểm
        PHÂN QUYỀN, nên giả lập đúng một bước và không hơn."""

        async def load_session(self, session_id: str) -> Principal | None:
            return principal if session_id == "phien-hop-le" else None

        async def upsert_user_and_create_session(self, claims: object, token: object) -> str:
            raise NotImplementedError

        async def delete_session(self, session_id: str) -> None:
            return None

    # `str(url)` CHE mật khẩu thành `***`, nên phải render tường minh — dùng
    # str() rồi vá lại sẽ cho một URL không mật khẩu và asyncpg báo
    # "password authentication failed", một lỗi không nói gì về nguyên nhân.
    database = Database(
        db_engine.url.render_as_string(hide_password=False),
        pool_size=2,
        max_overflow=0,
    )
    app = create_app(database=database, user_store=_Store())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"loom_session": "phien-hop-le"},
        ) as client:
            try:
                yield ApiWorld(client, db_engine, ws_a, ws_b, user_id, principal, grant, make_user)
            finally:
                async with maker() as session:
                    # Dọn audit theo ACTOR, không theo workspace: một test có thể
                    # tạo thêm workspace mới, và audit của nó vẫn giữ FK tới
                    # app_user — thiếu dòng này thì lệnh xoá user vỡ vì khoá ngoại
                    # và fixture im lặng để lại dữ liệu, đúng lỗi Task 19 đã gặp.
                    await session.execute(delete(AuditLog).where(AuditLog.actor_user_id == user_id))
                    for ws_id in (ws_a, ws_b):
                        await session.execute(delete(Item).where(Item.workspace_id == ws_id))
                        await session.execute(
                            delete(RoleAssignment).where(RoleAssignment.scope_id == ws_id)
                        )
                        await session.execute(delete(Workspace).where(Workspace.id == ws_id))
                    await session.execute(
                        delete(RoleAssignment).where(RoleAssignment.scope_id == DEFAULT_TENANT_ID)
                    )
                    if extra_users:
                        # Theo PRINCIPAL và theo ACTOR, không theo scope: một test có
                        # thể gán quyền cho người dùng phụ ở một phạm vi ngoài ws_a/ws_b.
                        await session.execute(
                            delete(RoleAssignment).where(
                                RoleAssignment.principal_user_id.in_(extra_users)
                            )
                        )
                        await session.execute(
                            delete(RoleAssignment).where(RoleAssignment.created_by.in_(extra_users))
                        )
                        await session.execute(
                            delete(AuditLog).where(AuditLog.actor_user_id.in_(extra_users))
                        )
                    await session.execute(delete(AppUser).where(AppUser.id == user_id))
                    if extra_users:
                        await session.execute(delete(AppUser).where(AppUser.id.in_(extra_users)))
                    await session.commit()
