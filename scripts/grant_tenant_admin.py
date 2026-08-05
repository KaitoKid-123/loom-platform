"""Gán vai trò admin cấp tenant — đường bootstrap cho admin ĐẦU TIÊN.

Mọi thứ khác trong Loom cấp quyền qua API, và API đòi người gọi đã có quyền. Cái
đầu tiên thì không có ai cấp được, nên nó phải đến từ ngoài hệ thống. Đây là chỗ đó.

Chạy từ host, dùng credential trong `deploy/local/aiven.env`:

    make grant-admin EMAIL=long@loom.local

Người dùng phải ĐĂNG NHẬP ÍT NHẤT MỘT LẦN trước: hàng `app_user` được tạo lúc
đăng nhập đầu tiên, và `role_assignment.principal_user_id` có khoá ngoại tới nó.
Script dừng với một câu tiếng người nếu chưa có, chứ không để asyncpg báo lỗi khoá
ngoại — thông báo đó không nói cho ai biết phải làm gì.

Idempotent: chạy hai lần không sinh hàng thứ hai. `uq_role_assignment_principal
_scope` là UNIQUE nên lần thứ hai sẽ vỡ nếu không dùng ON CONFLICT.
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from loom_api.db import build_sqlalchemy_url
from loom_api.models import DEFAULT_TENANT_ID, AppUser, RoleAssignment
from loom_core.config import get_settings
from loom_core.roles import Role


async def grant(email: str) -> int:
    engine = create_async_engine(build_sqlalchemy_url(get_settings()))
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            user = (
                await session.execute(select(AppUser).where(AppUser.email == email))
            ).scalar_one_or_none()
            if user is None:
                known = (
                    (await session.execute(select(AppUser.email).order_by(AppUser.email)))
                    .scalars()
                    .all()
                )
                print(f"Không có người dùng '{email}'.", file=sys.stderr)
                print(
                    "Hàng app_user sinh ra lúc đăng nhập ĐẦU TIÊN — "
                    "đăng nhập một lần rồi chạy lại.",
                    file=sys.stderr,
                )
                if known:
                    print(f"Đã đăng nhập: {', '.join(known)}", file=sys.stderr)
                return 1

            await session.execute(
                pg_insert(RoleAssignment)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    principal_type="user",
                    principal_user_id=user.id,
                    principal_group=None,
                    scope_type="tenant",
                    scope_id=DEFAULT_TENANT_ID,
                    role=str(Role.admin),
                    # Admin đầu tiên tự đứng tên người cấp cho mình. `created_by` có
                    # khoá ngoại tới app_user nên không để trống được, và ghi một
                    # người khác vào đó sẽ là một dòng audit không đúng sự thật.
                    created_by=user.id,
                )
                .on_conflict_do_update(
                    index_elements=[
                        "principal_user_id",
                        "principal_group",
                        "scope_type",
                        "scope_id",
                    ],
                    set_={"role": str(Role.admin)},
                )
            )
            await session.commit()
        print(f"{email} giờ là admin cấp tenant.")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="email của người dùng đã đăng nhập ít nhất một lần")
    return asyncio.run(grant(parser.parse_args().email))


if __name__ == "__main__":
    raise SystemExit(main())
