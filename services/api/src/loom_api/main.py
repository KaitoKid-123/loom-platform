from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from loom_api import VERSION
from loom_api.db import Database, build_sqlalchemy_url
from loom_api.errors import install_error_handlers
from loom_api.logging import configure_logging
from loom_api.middleware import RequestContextMiddleware
from loom_api.oidc_client import OIDCClient
from loom_api.oidc_verifier import IdTokenClaims, OIDCVerifier
from loom_api.routers import audit, auth, domains, health, items, roles, search, workspaces
from loom_api.user_store import PostgresUserStore, UserStore
from loom_core.config import get_settings

VerifyIdToken = Callable[[str], Awaitable[IdTokenClaims]]


def create_app(
    database: Database | None = None,
    user_store: UserStore | None = None,
    oidc_http: httpx.AsyncClient | None = None,
    verify_id_token: VerifyIdToken | None = None,
) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    # Cùng quy tắc sở hữu đã trả giá ở Task 5, giờ áp cho HAI tài nguyên:
    # chỉ đóng thứ mình tạo ra. Fixture test của chính task này tiêm oidc_http
    # vào, nên đóng vô điều kiện là đóng client của người khác.
    owns_db = database is None
    owns_http = oidc_http is None

    db = database or Database(
        build_sqlalchemy_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    http = oidc_http or httpx.AsyncClient()
    oidc_client = OIDCClient(settings, http)
    store = user_store or PostgresUserStore(db, settings.session_ttl_hours)

    if verify_id_token is None:
        verifier = OIDCVerifier(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            fetch_jwks=oidc_client.fetch_jwks,
        )
        verify_id_token = verifier.verify

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_http:
            await http.aclose()
        if owns_db:
            await db.dispose()

    # Tài liệu API CHỈ mở ở local. Từ Giai đoạn 1b, bề mặt API chính là mô hình
    # RBAC — mọi đường workspace/item/role/audit, tên tham số, hình dạng mọi
    # response. Với người chưa đăng nhập thì đó là một bản đồ trinh sát miễn phí,
    # và nó càng có giá trị theo mỗi endpoint ta thêm vào.
    #
    # Đóng bằng cách KHÔNG đăng ký route, chứ không phải đặt sau xác thực: một
    # route không tồn tại thì không có bề mặt nào để dò.
    is_local = settings.environment == "local"

    app = FastAPI(
        title="Loom API",
        version=VERSION,
        openapi_url="/api/v1/openapi.json" if is_local else None,
        docs_url="/api/v1/docs" if is_local else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.oidc_client = oidc_client
    app.state.user_store = store
    app.state.verify_id_token = verify_id_token

    # Phải là lệnh add_middleware() CUỐI CÙNG. Starlette bọc middleware theo thứ
    # tự đăng ký đảo ngược, nên cái thêm sau cùng chạy ngoài cùng — vào trước
    # (dọn sạch contextvars) và ra sau. Middleware nào cũng bind contextvars thì
    # phải đăng ký TRƯỚC dòng này, không bao giờ sau.
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(domains.router, prefix="/api/v1")
    app.include_router(workspaces.router, prefix="/api/v1")
    app.include_router(items.router, prefix="/api/v1")
    app.include_router(roles.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(audit.router, prefix="/api/v1")
    return app
