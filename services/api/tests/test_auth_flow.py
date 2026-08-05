import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

# Cùng thư mục test, không phải package: keypair + hàm ký của test_oidc_verifier
# dùng lại nguyên vẹn ở đây để chuỗi E2E dưới đây chạy trên một token KÝ THẬT.
from test_oidc_verifier import make_keypair, sign

from loom_api.main import create_app
from loom_api.oidc_verifier import IdTokenClaims, InvalidIdToken
from loom_core.schemas import Principal

DISCOVERY = {
    "issuer": "http://loom.localhost/dex",
    "authorization_endpoint": "http://loom.localhost/dex/auth",
    "token_endpoint": "http://loom.localhost/dex/token",
    "jwks_uri": "http://loom.localhost/dex/keys",
}

# Cố ý KHÔNG theo thứ tự tăng dần: /me chỉ được thấy danh sách đã sắp xếp nếu
# Principal thật sự chuẩn hoá nó.
TOKEN_GROUPS = ("data-eng", "admins")


class FakeUserStore:
    """Thay cho Postgres trong unit test — test_user_store.py lo phần schema thật.

    load_session() trả `Principal`, ĐÚNG như PostgresUserStore: fake mà trả
    IdTokenClaims sẽ vẫn làm /me xanh nhờ duck typing (hai kiểu có cùng ba
    trường), tức là toàn bộ chuỗi /me chưa từng nhìn thấy một Principal thật.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Principal] = {}
        self.upserts: list[IdTokenClaims] = []

    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str:
        self.upserts.append(claims)
        session_id = f"sess-{len(self.upserts)}"
        self.sessions[session_id] = Principal(
            user_id=uuid.uuid4(),
            subject=claims.subject,
            email=claims.email,
            display_name=claims.display_name,
            groups=claims.groups,
        )
        return session_id

    async def load_session(self, session_id: str) -> Principal | None:
        return self.sessions.get(session_id)

    async def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


class UnreconstructableSessionStore:
    """load_session() không dựng lại được danh tính — mô phỏng hàng DB hỏng
    (subject rỗng) mà PostgresUserStore.load_session() gặp khi đọc thẳng từ
    DB, đi vòng qua verify(). Xem probe thực tế trên Postgres thật ở review
    trước; từ Task 3 thì ValidationError của Principal được dịch thành
    InvalidIdToken đúng trên đường này."""

    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str:
        raise NotImplementedError

    async def load_session(self, session_id: str) -> Principal | None:
        raise InvalidIdToken("unusable_session_row")

    async def delete_session(self, session_id: str) -> None:
        pass


def oidc_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"id_token": "tok", "refresh_token": "rt"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.fixture
def store() -> FakeUserStore:
    return FakeUserStore()


@pytest.fixture
async def app_client(store: FakeUserStore):
    async def verify(_id_token: str) -> IdTokenClaims:
        return IdTokenClaims(
            subject="CgRsb25n",
            email="long@loom.local",
            display_name="Long",
            groups=TOKEN_GROUPS,
        )

    app = create_app(
        user_store=store,
        oidc_http=httpx.AsyncClient(transport=oidc_transport()),
        verify_id_token=verify,
    )
    # Mọi create_app() phải có lifespan tương ứng chạy (xem conftest.py's
    # `client` fixture và ghi chú ở test_request_context_middleware_is_outermost):
    # nếu không, engine/connection do create_app() dựng lên bị bỏ rơi. Fixture
    # này trước đây bỏ qua lifespan — vô hại hôm nay chỉ vì oidc_http được tiêm
    # (owns_http=False) và DB async engine không kết nối eager, nhưng đó chính
    # là kiểu sai lệch quy tắc đã gây rò rỉ engine ở Task 5.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://loom.localhost") as client:
            yield client


async def test_login_redirects_to_provider(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/auth/login")
    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert location.netloc == "loom.localhost"
    assert location.path == "/dex/auth"
    query = parse_qs(location.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"]


async def test_login_sets_transaction_cookie(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/auth/login")
    assert response.cookies.get("loom_oidc_tx")
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


async def test_callback_without_transaction_cookie_redirects_with_error(
    app_client: AsyncClient, store: FakeUserStore
) -> None:
    """callback chỉ bao giờ được tới qua redirect toàn trang từ Dex — không có
    caller lập trình nào. Cookie giao dịch hết hạn (khung 10 phút mà MFA hay
    một IdP liên kết hoàn toàn có thể vượt quá) không được để người dùng kẹt
    trên JSON thô; phải là một trang họ điều hướng được."""
    response = await app_client.get("/api/v1/auth/callback?code=x&state=y")
    assert response.status_code == 307
    assert response.headers["location"] == "/?error=login_failed"
    assert not store.upserts
    assert not app_client.cookies.get("loom_session")


async def test_callback_with_mismatched_state_redirects_with_error(
    app_client: AsyncClient, store: FakeUserStore
) -> None:
    await app_client.get("/api/v1/auth/login")
    response = await app_client.get("/api/v1/auth/callback?code=x&state=wrong-state")
    assert response.status_code == 307
    assert response.headers["location"] == "/?error=login_failed"
    assert not store.upserts
    assert not app_client.cookies.get("loom_session")


async def test_callback_with_exchange_failure_redirects_with_error(
    store: FakeUserStore,
) -> None:
    """Nhánh thứ ba callback có thể trả lỗi cho người dùng: exchange_code() bị
    nhà cung cấp từ chối (TokenExchangeError). Trước đây đây cũng là 400/401
    JSON thô; giờ phải redirect giống hai nhánh trên, và vẫn không tạo session."""

    async def verify(_id_token: str) -> IdTokenClaims:
        return IdTokenClaims(subject="CgRsb25n", email="long@loom.local", display_name="Long")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        if request.url.path.endswith("/token"):
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)

    app = create_app(
        user_store=store,
        oidc_http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        verify_id_token=verify,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://loom.localhost") as client:
            login = await client.get("/api/v1/auth/login")
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

            response = await client.get(f"/api/v1/auth/callback?code=bad&state={state}")

            assert response.status_code == 307
            assert response.headers["location"] == "/?error=login_failed"
            assert not store.upserts
            assert not client.cookies.get("loom_session")


async def test_full_login_flow_creates_session(
    app_client: AsyncClient, store: FakeUserStore
) -> None:
    login = await app_client.get("/api/v1/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

    callback = await app_client.get(f"/api/v1/auth/callback?code=the-code&state={state}")
    assert callback.status_code == 307
    assert callback.headers["location"] == "/"
    assert app_client.cookies.get("loom_session")
    assert store.upserts[0].email == "long@loom.local"


async def test_refresh_token_is_never_sent_to_browser(
    app_client: AsyncClient,
) -> None:
    login = await app_client.get("/api/v1/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    callback = await app_client.get(f"/api/v1/auth/callback?code=c&state={state}")
    assert "rt" not in callback.headers.get("set-cookie", "")
    assert "rt" not in callback.text


async def test_me_requires_session(app_client: AsyncClient) -> None:
    response = await app_client.get("/api/v1/me")
    assert response.status_code == 401


async def test_me_returns_current_user_after_login(app_client: AsyncClient) -> None:
    login = await app_client.get("/api/v1/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    await app_client.get(f"/api/v1/auth/callback?code=c&state={state}")

    response = await app_client.get("/api/v1/me")
    assert response.status_code == 200
    # So sánh CẢ đối tượng, không chỉ vài khoá: một trường bị bỏ quên trong
    # CurrentUser (hoặc thêm vào mà không ai định) làm test này đỏ.
    # `groups` đã sắp xếp trong khi token phát ra theo thứ tự khác — chứng minh
    # chuẩn hoá thật sự xảy ra trên đường này, không phải trùng hợp.
    assert response.json() == {
        "subject": "CgRsb25n",
        "email": "long@loom.local",
        "display_name": "Long",
        "groups": ["admins", "data-eng"],
    }
    assert list(TOKEN_GROUPS) != ["admins", "data-eng"], "input phải KHÁC output đã sắp xếp"


async def test_me_returns_401_when_session_row_is_unreconstructable() -> None:
    """load_session() có thể ném InvalidIdToken (hàng DB hỏng, ví dụ subject
    rỗng, đi vòng qua verify()). Với client thì phiên đơn giản không dùng
    được — phải là 401, không phải 500."""

    async def verify(_id_token: str) -> IdTokenClaims:
        return IdTokenClaims(subject="x", email="x@loom.local", display_name="X")

    app = create_app(
        user_store=UnreconstructableSessionStore(),
        oidc_http=httpx.AsyncClient(transport=oidc_transport()),
        verify_id_token=verify,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://loom.localhost") as client:
            client.cookies.set("loom_session", "whatever-session-id", domain="loom.localhost")
            response = await client.get("/api/v1/me")
            assert response.status_code == 401


# --------------------------------------------------------------------------
# Claim `groups` méo mó, đi HẾT đường thật: /auth/callback -> exchange_code ->
# OIDCVerifier THẬT (không phải verify_id_token giả) -> user_store.
#
# `groups: 123` từng ném TypeError bên trong verify(), sau khối try/except của
# nó, nên `except (TokenExchangeError, InvalidIdToken)` trong callback không
# bắt được và người dùng nhận HTTP 500 text/plain — trên chính đường đăng nhập,
# nơi mọi nhánh lỗi phải trả về một trang điều hướng được. Test này khoá lại
# quy tắc đó cho MỌI hình dạng claim, không chỉ ca đã biết.
# --------------------------------------------------------------------------

# Giá trị mặc định của Settings, đúng bằng cái OIDCVerifier mà create_app() tự
# dựng sẽ dùng. Khớp với iss/aud của token ký trong test_oidc_verifier.
_ISSUER = "http://loom.localhost/dex"
_CLIENT_ID = "loom"


def _signed_transport(id_token: str, jwks: dict[str, object]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"id_token": id_token, "refresh_token": "rt"})
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("groups_claim", "expected_groups"),
    [
        ("admins", ()),  # chuỗi trần — từng thành ('a','d','i','m','n','s')
        ("admins data-eng", ()),  # phân cách bởi dấu cách, hình dạng IdP thường gặp
        (123, ()),  # không iterate được — từng là TypeError -> 500
        (None, ()),  # null hợp lệ, nghĩa là không nhóm
        ({"admins": True}, ()),  # object JSON: iterate ra KHOÁ
        (["admins", 7, None, ""], ("admins",)),  # phần tử rác bị bỏ, phần tốt giữ lại
        (["admins", "data-eng"], ("admins", "data-eng")),  # ca đúng, để so sánh
    ],
    ids=["bare-string", "space-delimited", "integer", "null", "object", "mixed-list", "valid"],
)
async def test_callback_survives_any_shape_of_groups_claim(
    store: FakeUserStore, groups_claim: object, expected_groups: tuple[str, ...]
) -> None:
    key, jwks = make_keypair()
    token = sign(key, iss=_ISSUER, aud=_CLIENT_ID, groups=groups_claim)

    # verify_id_token KHÔNG được tiêm: create_app() dựng OIDCVerifier thật, nên
    # chuỗi được kiểm là chuỗi chạy trong production.
    app = create_app(
        user_store=store,
        oidc_http=httpx.AsyncClient(transport=_signed_transport(token, jwks)),
    )
    async with app.router.lifespan_context(app):
        # raise_app_exceptions=False: một exception lọt ra khỏi handler phải
        # hiện ra ở đây dưới dạng 500 để assert bên dưới bắt được, chứ không
        # phải nổ ngược vào test và cho một traceback khó đọc.
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://loom.localhost") as client:
            login = await client.get("/api/v1/auth/login")
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

            response = await client.get(f"/api/v1/auth/callback?code=c&state={state}")

    assert response.status_code != 500, response.text
    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert client.cookies.get("loom_session")
    assert store.upserts[0].groups == expected_groups


async def test_logout_clears_session(app_client: AsyncClient) -> None:
    login = await app_client.get("/api/v1/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    await app_client.get(f"/api/v1/auth/callback?code=c&state={state}")

    logout = await app_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert (await app_client.get("/api/v1/me")).status_code == 401
