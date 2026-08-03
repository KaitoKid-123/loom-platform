"""Giao thức OIDC — tách khỏi tầng HTTP để test được mà không cần dựng server."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt
import structlog
from jwt import PyJWK

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
