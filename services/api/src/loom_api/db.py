from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import URL
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
        return settings.database_url
    return URL.create(
        "postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    ).render_as_string(hide_password=False)


class Database:
    def __init__(self, url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url, pool_pre_ping=True, pool_size=5, max_overflow=5
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()
