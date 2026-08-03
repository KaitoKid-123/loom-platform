from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from loom_api.main import create_app
from loom_api.oidc_verifier import IdTokenClaims

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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://loom.localhost"
    ) as client:
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


async def test_callback_without_transaction_cookie_is_rejected(
    app_client: AsyncClient,
) -> None:
    response = await app_client.get("/api/v1/auth/callback?code=x&state=y")
    assert response.status_code == 400


async def test_callback_with_mismatched_state_is_rejected(
    app_client: AsyncClient,
) -> None:
    await app_client.get("/api/v1/auth/login")
    response = await app_client.get("/api/v1/auth/callback?code=x&state=wrong-state")
    assert response.status_code == 400


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


async def test_logout_clears_session(app_client: AsyncClient) -> None:
    login = await app_client.get("/api/v1/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    await app_client.get(f"/api/v1/auth/callback?code=c&state={state}")

    logout = await app_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert (await app_client.get("/api/v1/me")).status_code == 401
