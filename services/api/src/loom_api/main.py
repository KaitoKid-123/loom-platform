from fastapi import FastAPI

from loom_api import VERSION
from loom_api.logging import configure_logging
from loom_api.middleware import RequestContextMiddleware
from loom_api.routers import health
from loom_core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Loom API",
        version=VERSION,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router, prefix="/api/v1")
    return app
