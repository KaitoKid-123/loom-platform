"""Tiện ích PKCE và cookie ký. Không phụ thuộc FastAPI hay SQLAlchemy — không I/O.

Tầng chạm DB (UserStore/PostgresUserStore) nằm ở loom_api.user_store."""

import base64
import hashlib
import secrets
from typing import Any

from itsdangerous import URLSafeTimedSerializer


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def generate_pkce_pair() -> tuple[str, str]:
    """Trả về (code_verifier, code_challenge) theo RFC 7636 phương thức S256."""
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, challenge_for(verifier)


class CookieSigner:
    """Ký giá trị cookie. `salt` cô lập các loại cookie khác nhau."""

    def __init__(self, secret: str, salt: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt=salt)

    def dumps(self, value: Any) -> str:
        return self._serializer.dumps(value)

    def loads(self, token: str, max_age: int) -> Any:
        return self._serializer.loads(token, max_age=max_age)
