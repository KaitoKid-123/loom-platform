"""Tầng lưu trữ user/session — chạm Postgres. Tách khỏi sessions.py cùng lý do
Task 7 tách oidc.py: nửa thuần (PKCE, cookie ký) không cần biết gì về
SQLAlchemy, và PostgresUserStore đủ phức tạp để cần bộ test riêng."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from loom_api.db import Database
from loom_api.models import DEFAULT_TENANT_ID, AppUser, UserSession
from loom_api.oidc_verifier import IdTokenClaims, InvalidIdToken
from loom_core.schemas import Principal


class UserStore(Protocol):
    async def upsert_user_and_create_session(
        self, claims: IdTokenClaims, refresh_token: str | None
    ) -> str: ...

    async def load_session(self, session_id: str) -> Principal | None: ...

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
                groups=list(claims.groups),
                expires_at=now + self._ttl,
            )
            session.add(user_session)
            await session.commit()
            return str(user_session.id)

    async def load_session(self, session_id: str) -> Principal | None:
        try:
            key = uuid.UUID(session_id)
        except ValueError:
            return None

        async with self._db.session() as session:
            # one_or_none(), KHÔNG phải first(): lọc theo khoá chính của
            # user_session nên nhiều hơn một hàng là bất khả — và nếu điều bất khả
            # xảy ra thì phải nổ, không phải lặng lẽ chọn hàng đầu tiên. Giai đoạn 0
            # dùng scalar_one_or_none() ở đây; đừng nới nó ra khi chuyển sang select
            # hai thực thể.
            row = (
                await session.execute(
                    select(AppUser, UserSession.groups)
                    .join(UserSession, UserSession.user_id == AppUser.id)
                    .where(
                        UserSession.id == key,
                        UserSession.expires_at > datetime.now(UTC),
                    )
                )
            ).one_or_none()

        if row is None:
            return None
        user, groups = row
        try:
            return Principal(
                user_id=user.id,
                subject=user.subject,
                email=user.email,
                display_name=user.display_name,
                groups=tuple(groups or ()),
            )
        except ValidationError as exc:
            # Hàng DB không dựng lại được thành một danh tính hợp lệ (subject rỗng,
            # tên nhóm rỗng). Trước Task 3, IdTokenClaims.__post_init__ ném
            # InvalidIdToken ngay tại đây và cả /me lẫn deps.py bắt đúng nó để trả
            # 401. Dịch ValidationError sang cùng ngoại lệ đó, nếu không nhánh 401
            # ấy thành mã chết và một hàng hỏng đi thẳng ra thành 500.
            raise InvalidIdToken("unusable_session_row") from exc

    async def delete_session(self, session_id: str) -> None:
        try:
            key = uuid.UUID(session_id)
        except ValueError:
            return
        async with self._db.session() as session:
            await session.execute(delete(UserSession).where(UserSession.id == key))
            await session.commit()
