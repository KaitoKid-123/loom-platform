"""Tiện ích PKCE, cookie ký và tầng lưu trữ session. Phần trên không phụ thuộc
FastAPI; PostgresUserStore là phần duy nhất chạm DB."""

import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import delete, select, update

from loom_api.db import Database
from loom_api.models import DEFAULT_TENANT_ID, AppUser, UserSession
from loom_api.oidc_verifier import IdTokenClaims


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


class UserStore(Protocol):
    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str: ...

    async def load_session(self, session_id: str) -> IdTokenClaims | None: ...

    async def delete_session(self, session_id: str) -> None: ...


class PostgresUserStore:
    def __init__(self, db: Database, session_ttl_hours: int) -> None:
        self._db = db
        self._ttl = timedelta(hours=session_ttl_hours)

    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str:
        async with self._db.session() as session:
            user = (
                await session.execute(select(AppUser).where(AppUser.subject == claims.subject))
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if user is None:
                user = AppUser(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    subject=claims.subject,
                    email=claims.email,
                    display_name=claims.display_name,
                    last_login_at=now,
                )
                session.add(user)
            else:
                await session.execute(
                    update(AppUser)
                    .where(AppUser.id == user.id)
                    .values(
                        email=claims.email,
                        display_name=claims.display_name,
                        last_login_at=now,
                    )
                )

            user_session = UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                refresh_token=refresh_token,
                expires_at=now + self._ttl,
            )
            session.add(user_session)
            await session.commit()
            return str(user_session.id)

    async def load_session(self, session_id: str) -> IdTokenClaims | None:
        try:
            key = uuid.UUID(session_id)
        except ValueError:
            return None

        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(AppUser)
                    .join(UserSession, UserSession.user_id == AppUser.id)
                    .where(
                        UserSession.id == key,
                        UserSession.expires_at > datetime.now(UTC),
                    )
                )
            ).scalar_one_or_none()

        if row is None:
            return None
        return IdTokenClaims(subject=row.subject, email=row.email, display_name=row.display_name)

    async def delete_session(self, session_id: str) -> None:
        try:
            key = uuid.UUID(session_id)
        except ValueError:
            return
        async with self._db.session() as session:
            await session.execute(delete(UserSession).where(UserSession.id == key))
            await session.commit()
