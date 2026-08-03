from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from loom_api import VERSION
from loom_api.db import Database, build_sqlalchemy_url
from loom_api.logging import configure_logging
from loom_api.middleware import RequestContextMiddleware
from loom_api.routers import health
from loom_core.config import get_settings


def create_app(database: Database | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    db = database or Database(build_sqlalchemy_url(settings))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await db.dispose()

    app = FastAPI(
        title="Loom API",
        version=VERSION,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    # Phải là lệnh add_middleware() CUỐI CÙNG. Starlette bọc middleware theo thứ
    # tự đăng ký đảo ngược, nên cái thêm sau cùng chạy ngoài cùng — vào trước
    # (dọn sạch contextvars) và ra sau. Middleware nào cũng bind contextvars thì
    # phải đăng ký TRƯỚC dòng này, không bao giờ sau.
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router, prefix="/api/v1")
    return app
