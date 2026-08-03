from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from loom_api.main import create_app
from loom_api.oidc_verifier import IdTokenClaims, InvalidIdToken

DISCOVERY = {
    "issuer": "http://loom.localhost/dex",
    "authorization_endpoint": "http://loom.localhost/dex/auth",
    "token_endpoint": "http://loom.localhost/dex/token",
    "jwks_uri": "http://loom.localhost/dex/keys",
}


class FakeUserStore:
    """Thay cho Postgres trong unit test — Task 5 đã lo phần schema thật."""

    def __init__(self) -> None:
        self.sessions: dict[str, IdTokenClaims] = {}
        self.upserts: list[IdTokenClaims] = []

    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str:
        self.upserts.append(claims)
        session_id = f"sess-{len(self.upserts)}"
        self.sessions[session_id] = claims
        return session_id

    async def load_session(self, session_id: str) -> IdTokenClaims | None:
        return self.sessions.get(session_id)

    async def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


class UnreconstructableSessionStore:
    """load_session() không dựng lại được claims — mô phỏng hàng DB hỏng
    (subject rỗng) mà PostgresUserStore.load_session() gặp khi đọc thẳng từ
    DB, đi vòng qua verify(). Xem probe thực tế trên Postgres thật ở review
    trước: __post_init__ của IdTokenClaims ném InvalidIdToken đúng trên
    đường này."""

    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str:
        raise NotImplementedError

    async def load_session(self, session_id: str) -> IdTokenClaims | None:
        raise InvalidIdToken("empty_subject")

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
        return IdTokenClaims(subject="CgRsb25n", email="long@loom.local", display_name="Long")

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
    assert response.json() == {
        "subject": "CgRsb25n",
        "email": "long@loom.local",
        "display_name": "Long",
    }


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


async def test_logout_clears_session(app_client: AsyncClient) -> None:
    login = await app_client.get("/api/v1/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    await app_client.get(f"/api/v1/auth/callback?code=c&state={state}")

    logout = await app_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert (await app_client.get("/api/v1/me")).status_code == 401
