from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from loom_core.config import Settings


def build_sqlalchemy_url(settings: Settings) -> str:
    """URL rời rạc → chuỗi kết nối, escape đúng ký tự đặc biệt trong mật khẩu."""
    if settings.database_url:
        return _normalise_ssl_param(settings.database_url)
    return URL.create(
        "postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        # `ssl`, không phải `sslmode` — asyncpg từ chối tên kia bằng một
        # TypeError không hề nhắc tới TLS. Xem ghi chú ở Task 14.
        query={"ssl": settings.db_sslmode},
    ).render_as_string(hide_password=False)


def _normalise_ssl_param(url: str) -> str:
    """Đổi `sslmode` (cách libpq/Aiven viết) thành `ssl` (cách asyncpg hiểu).

    Aiven đưa URI có `?sslmode=require`. asyncpg từ chối tham số đó bằng
    `TypeError: connect() got an unexpected keyword argument 'sslmode'` — một
    thông báo không hề nói tới TLS. Giá trị thì trùng nhau hoàn toàn giữa hai
    cách viết, nên chỉ cần đổi tên khoá.
    """
    parsed = make_url(url)
    if "sslmode" not in parsed.query:
        return url
    query = dict(parsed.query)
    query["ssl"] = query.pop("sslmode")
    return parsed.set(query=query).render_as_string(hide_password=False)


class Database:
    def __init__(self, url: str, pool_size: int = 5, max_overflow: int = 5) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()
