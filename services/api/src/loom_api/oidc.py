"""Giao thức OIDC — tách khỏi tầng HTTP để test được mà không cần dựng server."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWK

JWKSFetcher = Callable[[], Awaitable[dict[str, Any]]]


class InvalidIdToken(Exception):
    """ID token không hợp lệ. Không bao giờ để lộ chi tiết ra ngoài response."""


@dataclass(frozen=True)
class IdTokenClaims:
    subject: str
    email: str
    display_name: str


class OIDCVerifier:
    def __init__(self, issuer: str, client_id: str, fetch_jwks: JWKSFetcher) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._fetch_jwks = fetch_jwks
        self._keys: dict[str, PyJWK] | None = None

    async def _load_keys(self) -> dict[str, PyJWK]:
        document = await self._fetch_jwks()
        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if kid:
                keys[kid] = PyJWK.from_dict(entry)
        return keys

    async def _key_for(self, kid: str) -> PyJWK:
        if self._keys is None:
            self._keys = await self._load_keys()
        if kid not in self._keys:
            # Nhà cung cấp có thể vừa xoay khoá — thử nạp lại đúng một lần.
            self._keys = await self._load_keys()
        key = self._keys.get(kid)
        if key is None:
            raise InvalidIdToken("không tìm thấy khoá ký tương ứng")
        return key

    async def verify(self, id_token: str) -> IdTokenClaims:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise InvalidIdToken("header của token hỏng") from exc

        kid = header.get("kid")
        if not kid:
            raise InvalidIdToken("token thiếu kid")

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
            raise InvalidIdToken("xác minh chữ ký hoặc claim thất bại") from exc

        subject = str(payload["sub"])
        email = str(payload.get("email") or "")
        return IdTokenClaims(
            subject=subject,
            email=email,
            display_name=str(payload.get("name") or email or subject),
        )
