"""Tầng lưu trữ user/session — chạm Postgres. Tách khỏi sessions.py cùng lý do
Task 7 tách oidc.py: nửa thuần (PKCE, cookie ký) không cần biết gì về
SQLAlchemy, và PostgresUserStore đủ phức tạp để cần bộ test riêng."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from loom_api.db import Database
from loom_api.models import DEFAULT_TENANT_ID, AppUser, UserSession
from loom_api.oidc_verifier import IdTokenClaims


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
            now = datetime.now(UTC)
            # SELECT-rồi-INSERT thua cuộc đua: hai lần đăng nhập đầu tiên đồng
            # thời của cùng một subject đều thấy chưa có hàng, đều chèn, và cái
            # sau vi phạm uq_app_user_subject → 500. ON CONFLICT làm nguyên tử.
            stmt = (
                pg_insert(AppUser)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    subject=claims.subject,
                    email=claims.email,
                    display_name=claims.display_name,
                    last_login_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[AppUser.subject],
                    set_={
                        "email": claims.email,
                        "display_name": claims.display_name,
                        "last_login_at": now,
                    },
                )
                .returning(AppUser.id)
            )
            user_id = (await session.execute(stmt)).scalar_one()

            user_session = UserSession(
                id=uuid.uuid4(),
                user_id=user_id,
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
