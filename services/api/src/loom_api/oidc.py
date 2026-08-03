"""Giao thức OIDC — tách khỏi tầng HTTP để test được mà không cần dựng server."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
import structlog
from jwt import PyJWK

from loom_core.config import Settings

JWKSFetcher = Callable[[], Awaitable[dict[str, Any]]]

logger = structlog.get_logger(__name__)


class InvalidIdToken(Exception):
    """ID token không hợp lệ.

    Thông điệp con người là một hằng số cố ý: nó có thể bị stringify ở bất kỳ
    đâu mà không tiết lộ giai đoạn xác minh nào đã thất bại. Lý do máy đọc nằm
    ở `.reason`, chỉ dùng cho log.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("id token verification failed")


@dataclass(frozen=True)
class IdTokenClaims:
    subject: str
    email: str
    display_name: str

    def __post_init__(self) -> None:
        # Bất biến nằm trên chính kiểu dữ liệu, không nằm ở verify(): Task 8
        # dựng lại IdTokenClaims thẳng từ hàng trong DB (load_session), tức là
        # đi vòng qua verify() hoàn toàn.
        if not self.subject.strip():
            raise InvalidIdToken("empty_subject")


class OIDCVerifier:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        fetch_jwks: JWKSFetcher,
        cache_ttl_seconds: float = 300.0,
        min_refresh_interval_seconds: float = 10.0,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._fetch_jwks = fetch_jwks
        self._cache_ttl_seconds = cache_ttl_seconds
        self._min_refresh_interval_seconds = min_refresh_interval_seconds
        self._keys: dict[str, PyJWK] | None = None
        self._loaded_at = 0.0
        self._reload_lock = asyncio.Lock()

    async def _load_keys(self) -> dict[str, PyJWK]:
        document = await self._fetch_jwks()
        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except Exception as exc:
                # Một khoá hỏng không được phép hạ gục các khoá còn tốt.
                logger.warning("oidc.jwks_entry_invalid", kid=kid, error=type(exc).__name__)
        return keys

    def _needs_reload(self, kid: str) -> bool:
        if self._keys is None:
            return True
        age = time.monotonic() - self._loaded_at
        if age >= self._cache_ttl_seconds:
            return True
        # kid lạ được phép nạp lại sớm, nhưng có sàn thời gian: nếu không, kẻ
        # gửi kid rác ép được đúng một lần fetch cho mỗi request.
        return kid not in self._keys and age >= self._min_refresh_interval_seconds

    async def _key_for(self, kid: str) -> PyJWK:
        if self._needs_reload(kid):
            async with self._reload_lock:
                # Kiểm lại sau khi giành được khoá: một coroutine khác có thể
                # vừa nạp xong trong lúc ta chờ.
                if self._needs_reload(kid):
                    self._keys = await self._load_keys()
                    self._loaded_at = time.monotonic()

        key = (self._keys or {}).get(kid)
        if key is None:
            raise InvalidIdToken("unknown_kid")
        return key

    async def verify(self, id_token: str) -> IdTokenClaims:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise InvalidIdToken("malformed_header") from exc

        kid = header.get("kid")
        if not kid:
            raise InvalidIdToken("missing_kid")

        key = await self._key_for(kid)
        try:
            payload = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidIdToken("verification_failed") from exc
        except Exception as exc:
            # PyJWT ném được cả thứ không phải PyJWTError (ví dụ TypeError từ
            # force_bytes khi khoá sai kiểu). Không để nó thoát ra thành 500 —
            # nhưng phải mang mã lý do RIÊNG, vì "PyJWT từ chối token" và
            # "có gì đó hỏng bất ngờ" là hai chuyện khác nhau, và test canh
            # allow-list dựa vào đúng sự khác biệt này.
            logger.warning("oidc.verify_unexpected_error", error=type(exc).__name__)
            raise InvalidIdToken("unexpected_error") from exc

        subject = str(payload["sub"])
        email = str(payload.get("email") or "")
        return IdTokenClaims(
            subject=subject,
            email=email,
            display_name=str(payload.get("name") or email or subject),
        )


class TokenExchangeError(Exception):
    """Nhà cung cấp OIDC từ chối đổi code lấy token."""


@dataclass(frozen=True)
class OIDCEndpoints:
    authorization_endpoint: str  # địa chỉ công khai — trình duyệt dùng
    token_endpoint: str  # địa chỉ nội bộ — pod dùng
    jwks_uri: str  # địa chỉ nội bộ — pod dùng


@dataclass(frozen=True)
class TokenSet:
    id_token: str
    access_token: str | None
    refresh_token: str | None


SCOPES = "openid email profile offline_access"


class OIDCClient:
    """Nói chuyện với nhà cung cấp OIDC.

    Trình duyệt và pod nhìn Dex ở hai địa chỉ khác nhau. Endpoint dành cho
    trình duyệt giữ nguyên địa chỉ công khai; endpoint gọi từ server được
    viết lại sang địa chỉ nội bộ trong cụm.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._endpoints: OIDCEndpoints | None = None

    def _to_internal(self, url: str) -> str:
        internal = self._settings.oidc_internal_base
        public = self._settings.public_base_url
        if not internal:
            return url
        # Kiểm biên chứ không startswith trần: "http://loom.localhost" là tiền
        # tố chuỗi của "http://loom.localhost.evil.example", nên startswith sẽ
        # viết lại tên miền của kẻ tấn công thành địa chỉ nội bộ — và
        # exchange_code sẽ POST client_secret sang đó.
        if url == public or url.startswith(public + "/"):
            return internal + url[len(public) :]
        logger.info("oidc.endpoint_not_rewritten", url=url, public_base=public)
        return url

    async def endpoints(self) -> OIDCEndpoints:
        if self._endpoints is not None:
            return self._endpoints

        discovery_url = self._to_internal(
            f"{self._settings.oidc_issuer}/.well-known/openid-configuration"
        )
        # 5s chứ không phải 10s: đây là đường đi của một redirect người dùng đang chờ.
        # Dex sống thì mọi lời gọi này ở mức mili-giây; Dex chết thì hỏng nhanh tốt hơn
        # treo lâu. Giai đoạn 0 chưa có circuit breaker.
        response = await self._http.get(discovery_url, timeout=5.0)
        response.raise_for_status()
        document = response.json()

        self._endpoints = OIDCEndpoints(
            authorization_endpoint=document["authorization_endpoint"],
            token_endpoint=self._to_internal(document["token_endpoint"]),
            jwks_uri=self._to_internal(document["jwks_uri"]),
        )
        return self._endpoints

    async def authorization_url(self, state: str, code_challenge: str) -> str:
        endpoints = await self.endpoints()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.oidc_client_id,
                "redirect_uri": self._settings.oidc_redirect_url,
                "scope": SCOPES,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoints.authorization_endpoint}?{query}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        endpoints = await self.endpoints()
        response = await self._http.post(
            endpoints.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.oidc_redirect_url,
                "client_id": self._settings.oidc_client_id,
                "client_secret": self._settings.oidc_client_secret,
                "code_verifier": code_verifier,
            },
            timeout=5.0,
        )
        if response.status_code != 200:
            raise TokenExchangeError(f"nhà cung cấp trả về {response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            raise TokenExchangeError("phản hồi không phải JSON hợp lệ") from exc

        if not isinstance(payload, dict) or "id_token" not in payload:
            raise TokenExchangeError("phản hồi thiếu id_token")
        return TokenSet(
            id_token=payload["id_token"],
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
        )

    async def fetch_jwks(self) -> dict[str, Any]:
        endpoints = await self.endpoints()
        try:
            response = await self._http.get(endpoints.jwks_uri, timeout=5.0)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
        except Exception as exc:
            # Task 6 đã bịt lỗi *entry* JWKS hỏng, nhưng lỗi *vận chuyển* (Dex
            # không tới được, timeout, JSON rác) vẫn thoát nguyên si khỏi
            # verify() và thành 500. Gói lại thành InvalidIdToken để handler
            # của Task 8 bắt được và trả 401.
            logger.warning("oidc.jwks_fetch_failed", error=type(exc).__name__)
            raise InvalidIdToken("jwks_unavailable") from exc
        return result
