import httpx
import pytest

from loom_api.oidc import InvalidIdToken, OIDCClient, TokenExchangeError
from loom_core.config import Settings

DISCOVERY = {
    "issuer": "http://loom.localhost/dex",
    "authorization_endpoint": "http://loom.localhost/dex/auth",
    "token_endpoint": "http://loom.localhost/dex/token",
    "jwks_uri": "http://loom.localhost/dex/keys",
}

SETTINGS = Settings(
    oidc_issuer="http://loom.localhost/dex",
    oidc_internal_base="http://dex.loom.svc.cluster.local:5556",
    oidc_client_id="loom",
    oidc_client_secret="loom-dev-secret",
    oidc_redirect_url="http://loom.localhost/api/v1/auth/callback",
    public_base_url="http://loom.localhost",
)


def make_client(handler: httpx.MockTransport) -> OIDCClient:
    return OIDCClient(SETTINGS, httpx.AsyncClient(transport=handler))


async def test_discovery_is_fetched_from_internal_address() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=DISCOVERY)

    client = make_client(httpx.MockTransport(handle))
    await client.endpoints()
    assert seen == ["http://dex.loom.svc.cluster.local:5556/dex/.well-known/openid-configuration"]


async def test_authorization_endpoint_stays_public() -> None:
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json=DISCOVERY)))
    endpoints = await client.endpoints()
    assert endpoints.authorization_endpoint == "http://loom.localhost/dex/auth"


async def test_server_side_endpoints_are_rewritten_to_internal() -> None:
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json=DISCOVERY)))
    endpoints = await client.endpoints()
    assert endpoints.token_endpoint == "http://dex.loom.svc.cluster.local:5556/dex/token"
    assert endpoints.jwks_uri == "http://dex.loom.svc.cluster.local:5556/dex/keys"


async def test_no_rewrite_when_internal_base_is_unset() -> None:
    settings = SETTINGS.model_copy(update={"oidc_internal_base": None})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=DISCOVERY))
    client = OIDCClient(settings, httpx.AsyncClient(transport=transport))
    endpoints = await client.endpoints()
    assert endpoints.token_endpoint == "http://loom.localhost/dex/token"


async def test_trailing_slash_in_public_base_url_does_not_disable_rewrite() -> None:
    """`_to_internal` so khớp biên bằng `startswith(public + "/")`.

    Nếu `public_base_url` có dấu / thừa ở cuối ("http://loom.localhost/"),
    phép so khớp đó sẽ thành hai gạch chéo liên tiếp và không khớp cả
    token_endpoint hợp lệ ("http://loom.localhost/dex/token") — tắt câm
    split-horizon mà không báo lỗi gì, vì pod sẽ dùng thẳng địa chỉ công khai
    không phân giải được từ trong cụm. `Settings._strip_trailing_slash` phải
    dọn dấu / này NGAY tại tầng cấu hình để mọi nơi dùng public_base_url đều
    được lợi, không chỉ `_to_internal`.

    Dùng `Settings(...)` (không phải `model_copy`) vì `model_copy(update=...)`
    của pydantic KHÔNG chạy lại field_validator trên các trường được cập nhật.
    """
    settings = Settings(
        oidc_issuer="http://loom.localhost/dex",
        oidc_internal_base="http://dex.loom.svc.cluster.local:5556",
        oidc_client_id="loom",
        oidc_client_secret="loom-dev-secret",
        oidc_redirect_url="http://loom.localhost/api/v1/auth/callback",
        public_base_url="http://loom.localhost/",  # dấu / thừa ở cuối, cố ý
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=DISCOVERY))
    client = OIDCClient(settings, httpx.AsyncClient(transport=transport))

    endpoints = await client.endpoints()

    assert endpoints.token_endpoint == "http://dex.loom.svc.cluster.local:5556/dex/token"


async def test_discovery_is_cached() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=DISCOVERY)

    client = make_client(httpx.MockTransport(handle))
    await client.endpoints()
    await client.endpoints()
    assert calls == 1


async def test_authorization_url_contains_pkce_and_state() -> None:
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json=DISCOVERY)))
    url = await client.authorization_url(state="st4te", code_challenge="ch4llenge")
    assert url.startswith("http://loom.localhost/dex/auth?")
    assert "code_challenge=ch4llenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=st4te" in url
    assert "client_id=loom" in url
    assert "scope=openid+email+profile+offline_access" in url
    assert "response_type=code" in url


async def test_exchange_code_posts_credentials_and_verifier() -> None:
    captured: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(
            200,
            json={"id_token": "the-id-token", "access_token": "at", "refresh_token": "rt"},
        )

    client = make_client(httpx.MockTransport(handle))
    tokens = await client.exchange_code(code="the-code", code_verifier="the-verifier")

    assert tokens.id_token == "the-id-token"
    assert tokens.refresh_token == "rt"
    assert captured["grant_type"] == "authorization_code"
    assert captured["code"] == "the-code"
    assert captured["code_verifier"] == "the-verifier"
    assert captured["client_id"] == "loom"
    assert captured["client_secret"] == "loom-dev-secret"
    assert captured["redirect_uri"] == "http://loom.localhost/api/v1/auth/callback"


async def test_exchange_code_raises_on_provider_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = make_client(httpx.MockTransport(handle))
    with pytest.raises(TokenExchangeError):
        await client.exchange_code(code="bad", code_verifier="v")


def _malformed_body_client(content: bytes) -> OIDCClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(200, content=content)

    return make_client(httpx.MockTransport(handle))


async def test_exchange_code_raises_on_non_json_body() -> None:
    """Task 6 bịt lỗi này cho fetch_jwks() nhưng chưa từng áp cho exchange_code():
    một body 200 không phải JSON trước đây thoát nguyên si thành
    json.decoder.JSONDecodeError, không phải TokenExchangeError mà handler của
    Task 8 biết bắt.
    """
    client = _malformed_body_client(b"not json at all")
    with pytest.raises(TokenExchangeError):
        await client.exchange_code(code="c", code_verifier="v")


async def test_exchange_code_raises_on_json_array_body() -> None:
    client = _malformed_body_client(b'["a", "b"]')
    with pytest.raises(TokenExchangeError):
        await client.exchange_code(code="c", code_verifier="v")


async def test_exchange_code_raises_on_json_scalar_body() -> None:
    """Trước bản vá: `"id_token" not in 42` ném TypeError trần, không phải
    TokenExchangeError."""
    client = _malformed_body_client(b"42")
    with pytest.raises(TokenExchangeError):
        await client.exchange_code(code="c", code_verifier="v")


async def test_exchange_code_raises_on_json_null_body() -> None:
    """Trước bản vá: `"id_token" not in None` ném TypeError trần, không phải
    TokenExchangeError."""
    client = _malformed_body_client(b"null")
    with pytest.raises(TokenExchangeError):
        await client.exchange_code(code="c", code_verifier="v")


async def test_fetch_jwks_uses_internal_address() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(200, json={"keys": []})

    client = make_client(httpx.MockTransport(handle))
    document = await client.fetch_jwks()
    assert document == {"keys": []}
    assert seen[-1] == "http://dex.loom.svc.cluster.local:5556/dex/keys"


async def test_fetch_jwks_wraps_transport_failure() -> None:
    """Task 6 review: verify() bọc jwt.decode(), nhưng không ai bọc fetch_jwks().

    Dex không tới được (ConnectError) phải nổi lên thành InvalidIdToken —
    không phải ConnectError trần — để handler của Task 8 bắt được và trả 401
    thay vì 500.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(httpx.MockTransport(handle))
    with pytest.raises(InvalidIdToken) as caught:
        await client.fetch_jwks()
    assert caught.value.reason == "jwks_unavailable"
    # Nguyên nhân gốc vẫn được giữ lại qua `raise ... from exc` để debug, nhưng
    # không phải là kiểu ngoại lệ mà caller phải bắt.
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


async def test_prefix_collision_host_is_left_untouched() -> None:
    """`_to_internal` phải kiểm biên, không chỉ str.startswith() trần.

    "http://loom.localhost" là tiền tố CHUỖI của
    "http://loom.localhost.evil.example" dù đây là hai gốc (origin) khác nhau
    hoàn toàn. Trước bản vá biên giới, `_to_internal` match nhầm chuỗi này và
    viết đè thành "http://dex.loom.svc.cluster.local:5556.evil.example/token"
    — vô hại hôm đó chỉ vì oidc_internal_base kết thúc bằng số cổng (":5556")
    khiến httpx từ chối URL kết quả (cổng không còn là số), KHÔNG phải vì có
    biên giới nào được kiểm. Đổi oidc_internal_base sang một hostname không
    cổng (hoàn toàn hợp lý ở một triển khai thật) thì phần đuôi
    ".evil.example" ghép ngay sau sẽ tạo ra một hostname hợp lệ mà kẻ tấn
    công kiểm soát được qua DNS — và exchange_code() sẽ POST client_secret
    thẳng sang đó.

    Sau bản vá: so khớp bằng `==` hoặc `startswith(public + "/")`, nên domain
    giả này không khớp gốc công khai thật — bị bỏ qua nguyên vẹn, xử lý giống
    hệt một host hoàn toàn không liên quan (xem
    test_endpoint_on_unrelated_host_is_left_untouched).
    """
    discovery = dict(DISCOVERY)
    discovery["token_endpoint"] = "http://loom.localhost.evil.example/token"
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json=discovery)))

    endpoints = await client.endpoints()

    # Không bị viết đè — domain của kẻ tấn công được giữ nguyên xi, không có
    # rewrite nào âm thầm đưa client_secret đi sai chỗ.
    assert endpoints.token_endpoint == "http://loom.localhost.evil.example/token"


async def test_endpoint_on_unrelated_host_is_left_untouched() -> None:
    """Dex quảng cáo endpoint ở một host hoàn toàn khác public_base_url.

    Không có tiền tố chung nên `_to_internal` không viết đè — đúng, vì không
    có cách nào đoán ra địa chỉ nội bộ tương ứng. Pod sẽ cố gọi ra thẳng địa
    chỉ công khai này; nay có một dòng log INFO ("oidc.endpoint_not_rewritten")
    ghi lại việc đó để không âm thầm mất dấu khi debug sự cố mạng trong cụm.
    """
    discovery = dict(DISCOVERY)
    discovery["token_endpoint"] = "https://accounts.example.com/token"
    client = make_client(httpx.MockTransport(lambda r: httpx.Response(200, json=discovery)))

    endpoints = await client.endpoints()

    assert endpoints.token_endpoint == "https://accounts.example.com/token"
