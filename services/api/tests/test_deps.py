"""Dependency xác thực dùng chung — bộ test.

Tên cookie ở đây CỐ Ý khác giá trị mặc định `loom_session` của Settings. Nếu
`deps.py` hardcode tên cookie thay vì đọc `settings.session_cookie_name` thì mọi
test dưới đây phải đỏ; nếu test dùng đúng tên mặc định thì một cái hardcode sống
sót nguyên vẹn và bộ test không kiểm gì cả.
"""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep, get_db_session, get_principal
from loom_api.errors import install_error_handlers
from loom_api.oidc_verifier import InvalidIdToken
from loom_core.schemas import Principal

COOKIE = "loom_test_cookie"

# KHÔNG phải một UUID: id phiên là chuỗi đối với dependency, việc phân tích nó
# thuộc PostgresUserStore. Chữ HOA lẫn thường là cố ý — nếu deps.py chuẩn hoá
# (`.lower()`, cắt bớt, strip) thì store nhận được chuỗi khác và assert
# store.calls đỏ; với một chuỗi toàn chữ thường thì `.lower()` sống sót.
SESSION_ID = "An-Opaque-SESSION-id"

PRINCIPAL = Principal(
    user_id=uuid.UUID("6e6f6e63-0000-4000-8000-000000000001"),
    subject="CgRsb25n",
    email="long@loom.local",
    display_name="Long",
    # Không theo thứ tự tăng dần, giống test_auth_flow: nếu dependency dựng lại
    # một Principal mới thay vì truyền nguyên bản thì so sánh dưới đây đỏ.
    groups=("data-eng", "admins"),
)


class FakeStore:
    """Chỉ có load_session — đúng phần UserStore mà get_principal được phép dùng."""

    def __init__(self, principal: Principal | None = None, error: Exception | None = None) -> None:
        self._principal = principal
        self._error = error
        self.calls: list[str] = []

    async def load_session(self, session_id: str) -> Principal | None:
        self.calls.append(session_id)
        if self._error is not None:
            raise self._error
        return self._principal


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeDatabase:
    """Thay Database. `session()` là async context manager, ĐÚNG như bản thật —
    một fake trả thẳng session (không phải context manager) sẽ làm test xanh
    trong khi deps.py bỏ mất `async with` và rò rỉ connection."""

    def __init__(self) -> None:
        self.handed_out: list[FakeSession] = []

    @asynccontextmanager
    async def session(self):
        created = FakeSession()
        self.handed_out.append(created)
        try:
            yield created
        finally:
            created.closed = True


def make_app(
    principal: Principal | None = None, error: Exception | None = None
) -> tuple[FastAPI, FakeStore, FakeDatabase, list[object]]:
    app = FastAPI()
    install_error_handlers(app)
    store = FakeStore(principal, error)
    db = FakeDatabase()

    class S:
        session_cookie_name = COOKIE

    app.state.settings = S()
    app.state.user_store = store
    app.state.db = db

    # Những gì handler THẬT SỰ nhận được, giữ nguyên identity chứ không chỉ giá
    # trị: một dependency dựng lại Principal bằng tay có thể vẫn so sánh bằng.
    received: list[object] = []

    @app.get("/protected")
    async def protected(p: Principal = PrincipalDep) -> dict[str, object]:
        received.append(p)
        return p.model_dump(mode="json")

    @app.get("/dbwork")
    async def dbwork(session: AsyncSession = SessionDep) -> dict[str, bool]:
        received.append(session)
        return {"ok": True}

    @app.get("/twice")
    async def twice(
        first: Principal = PrincipalDep, second: Principal = PrincipalDep
    ) -> dict[str, bool]:
        return {"same": first is second}

    @app.get("/echo-cookies")
    async def echo_cookies(request: Request) -> dict[str, str]:
        return dict(request.cookies)

    return app, store, db, received


async def call(
    app: FastAPI,
    path: str = "/protected",
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
):
    # cookies đặt trên client, không phải trên từng request: httpx đã deprecate
    # `client.get(cookies=...)`.
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=cookies or {}
    ) as ac:
        return await ac.get(path, headers=headers)


async def test_no_cookie_is_401_problem_json() -> None:
    app, store, _db, _seen = make_app(PRINCIPAL)
    response = await call(app)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    # KHÔNG được gọi tới store: không có cookie thì không có gì để tra, và một
    # truy vấn vô ích trên mọi request của khách chưa đăng nhập là một vector DoS
    # rẻ. Xem Step 6 của Task 4.
    assert store.calls == []


async def test_empty_cookie_value_is_401_without_touching_store() -> None:
    """`Cookie: loom_test_cookie=` cho `cookies.get()` trả về "" chứ không phải
    None. Nhánh kiểm phải là `if not session_id`, không phải `is None` — nếu
    không thì chuỗi rỗng vẫn đi thẳng xuống database."""
    app, store, _db, _seen = make_app(PRINCIPAL)
    headers = {"Cookie": f"{COOKIE}="}

    # Chứng minh cookie rỗng THẬT SỰ tới được app trước đã. Không có bước này,
    # `store.calls == []` bên dưới có thể xanh chỉ vì httpx/Starlette đã loại
    # cookie rỗng từ đầu — tức là test không kiểm gì.
    echo = await call(app, "/echo-cookies", headers=headers)
    assert echo.json() == {COOKIE: ""}

    response = await call(app, headers=headers)
    assert response.status_code == 401
    assert store.calls == []


async def test_cookie_name_comes_from_settings() -> None:
    """Gửi đúng tên mặc định của Settings (`loom_session`) trong khi settings của
    app nói tên khác: phải là 401 và không tra store. Test này chết nếu deps.py
    hardcode "loom_session"."""
    app, store, _db, _seen = make_app(PRINCIPAL)
    response = await call(app, cookies={"loom_session": SESSION_ID})
    assert response.status_code == 401
    assert store.calls == []


async def test_unknown_session_is_401() -> None:
    app, store, _db, _seen = make_app(None)
    response = await call(app, cookies={COOKIE: SESSION_ID})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    # Lần này store PHẢI được gọi — nếu không thì 401 ở trên đến từ nhánh
    # "không có cookie" và nhánh principal-is-None chưa từng chạy.
    assert store.calls == [SESSION_ID]


async def test_unusable_session_row_is_401_not_500() -> None:
    """Từ Task 3, `PostgresUserStore.load_session()` dịch ValidationError của
    Principal thành `InvalidIdToken("unusable_session_row")` — nên nhánh
    `except InvalidIdToken` ở đây là mã SỐNG, không phải mã chết. Một hàng phiên
    hỏng phải thành 401 (phiên không dùng được) chứ không phải 500."""
    app, store, _db, _seen = make_app(error=InvalidIdToken("unusable_session_row"))
    response = await call(app, cookies={COOKIE: SESSION_ID})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert store.calls == [SESSION_ID]


async def test_valid_session_passes_the_whole_principal_through() -> None:
    app, store, _db, received = make_app(PRINCIPAL)
    response = await call(app, cookies={COOKIE: SESSION_ID})

    assert response.status_code == 200
    # CHÍNH đối tượng store trả về, không phải một Principal bằng nhau: khoá RBAC
    # ở Task 9+ đọc user_id và groups từ đây, nên mọi trường phải đi qua nguyên vẹn.
    assert received == [PRINCIPAL]
    assert received[0] is PRINCIPAL
    assert response.json() == {
        "user_id": "6e6f6e63-0000-4000-8000-000000000001",
        "subject": "CgRsb25n",
        "email": "long@loom.local",
        "display_name": "Long",
        "groups": ["admins", "data-eng"],
    }
    assert store.calls == [SESSION_ID]


async def test_principal_is_resolved_once_per_request() -> None:
    """Giai đoạn 1 sẽ xếp dependency phân quyền LÊN TRÊN get_principal, nên một
    endpoint dễ dàng phụ thuộc nó hai lần. FastAPI cache dependency trong phạm vi
    request; nếu ai đó tắt cache (`Depends(..., use_cache=False)`) thì mỗi lần
    phụ thuộc là thêm một round-trip database cho cùng một phiên."""
    app, store, _db, _seen = make_app(PRINCIPAL)
    response = await call(app, "/twice", cookies={COOKIE: SESSION_ID})
    assert response.status_code == 200
    assert response.json() == {"same": True}
    assert store.calls == [SESSION_ID]


async def test_db_session_dependency_hands_over_the_database_session() -> None:
    app, _store, db, received = make_app()
    response = await call(app, "/dbwork")
    assert response.status_code == 200
    assert len(db.handed_out) == 1
    assert received[0] is db.handed_out[0]


async def test_db_session_dependency_does_not_commit() -> None:
    """Cố ý KHÔNG commit: hàng audit phải nằm trong CÙNG transaction với thao tác
    nó mô tả (spec mục 5.4), nên endpoint nào ghi thì tự commit sau khi đã ghi cả
    audit. Một commit ngầm ở tầng dependency sẽ phá điều đó một cách âm thầm —
    test này là thứ làm nó không âm thầm."""
    app, _store, db, _received = make_app()
    await call(app, "/dbwork")
    assert db.handed_out[0].commits == 0
    assert db.handed_out[0].rollbacks == 0


async def test_db_session_is_closed_when_the_request_ends() -> None:
    """`async with`, không phải gọi trần: nếu không thoát context manager thì mỗi
    request giữ một connection của pool mãi mãi."""
    app, _store, db, _received = make_app()
    await call(app, "/dbwork")
    assert db.handed_out[0].closed is True


async def test_db_session_is_one_per_request() -> None:
    app, _store, db, received = make_app()
    await call(app, "/dbwork")
    await call(app, "/dbwork")
    assert len(db.handed_out) == 2
    assert received[0] is not received[1]


def test_aliases_point_at_the_dependency_functions() -> None:
    """Các endpoint ở trên dùng PrincipalDep/SessionDep, nên chúng đã được chạy
    thật. Assert này chốt thêm rằng alias trỏ đúng hàm — đổi chỗ hai cái sẽ làm
    một loạt test trên đỏ, nhưng nói ra ở đây thì lý do đỏ rõ ràng hơn."""
    assert PrincipalDep.dependency is get_principal
    assert SessionDep.dependency is get_db_session
