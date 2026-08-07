"""`create_app()` — khuôn theo `loom_api.main`, xem docstring ở đó cho lý do
factory (`uvicorn ... --factory`): app chỉ dựng khi tiến trình thật sự khởi
động, không phải lúc import module.

`loom-query` KHÔNG có OIDC/session riêng và không dựng một mặt xác thực thứ
hai — Giai đoạn 1 chỉ hứa MỘT mặt xác thực (`loom-api`, cookie phiên httpOnly).
Principal của người dùng cuối tới qua thân request (`schemas.QueryCreate.
principal`), một giải pháp TẠM của Giai đoạn 2b — xem docstring ở đó.

`loom-query` cũng không có database: mọi trạng thái query là `QueryStore`
trong bộ nhớ tiến trình (xem `store.py`), và mọi câu hỏi về quyền đi qua HTTP
tới `loom-api` (`authz.py`) — không có SQLAlchemy, không có session, không có
migration nào ở service này.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import httpx
from fastapi import FastAPI

from loom_query import VERSION
from loom_query.authz import AuthzClient, AuthzPort, LakehouseResolver
from loom_query.config import Settings, get_settings
from loom_query.routers import health, lakehouses, query
from loom_query.store import QueryStore
from loom_storage import MinioStsProvider, StorageCredentials


def create_app(
    settings: Settings | None = None,
    authz: AuthzPort | None = None,
    resolver: LakehouseResolver | None = None,
    authz_http: httpx.AsyncClient | None = None,
    query_store: QueryStore | None = None,
    storage: StorageCredentials | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = query_store or QueryStore()

    # `storage=` cho test tiêm một `MinioStsProvider` trỏ vào MinIO testcontainer
    # thật (`tests/integration/`), hoặc một fake thuần Python cho test không
    # cần I/O. Mặc định dựng `MinioStsProvider` THẬT — KHÔNG cần `owns_http`-
    # kiểu ownership tracking như `authz`/`http` phía dưới: `MinioStsProvider`
    # không giữ resource nào cần đóng ở `lifespan` (chỉ một `boto3.client`
    # "sts", không mở kết nối lúc khởi tạo — đã kiểm bằng thực nghiệm, xem báo
    # cáo hoàn tất Task 13), nên dựng nó vô điều kiện là an toàn và rẻ.
    resolved_storage: StorageCredentials = storage or MinioStsProvider(
        endpoint_url=settings.s3_endpoint,
        bucket=settings.storage_bucket,
        root_access_key=settings.storage_root_access_key,
        root_secret_key=settings.storage_root_secret_key,
    )

    # Cùng quy tắc sở hữu của `loom_api.main.create_app`: chỉ đóng client HTTP
    # mà CHÍNH hàm này tạo ra. Khi test tiêm hẳn một `AuthzPort` giả (`authz=`),
    # không có `httpx.AsyncClient` nào được dựng ở đây — dựng một cái rồi
    # không bao giờ dùng chỉ để đóng nó lúc shutdown là lãng phí và là một
    # cảnh báo "unclosed client" giả trong log test.
    owns_http = False
    http: httpx.AsyncClient | None = None
    resolved_authz: AuthzPort
    if authz is not None:
        resolved_authz = authz
    else:
        owns_http = authz_http is None
        http = authz_http or httpx.AsyncClient()
        resolved_authz = AuthzClient(http=http, base_url=settings.authz_base_url)

    # `AuthzClient` cài CẢ HAI Protocol trên CÙNG một `base_url` (xem docstring
    # `authz.py`) — "hỏi quyền" và "dịch tên" đều là chuyện của `loom-api`. Khi
    # không có `resolver=` riêng, dùng lại CHÍNH `resolved_authz`: một
    # `AuthzClient` thật luôn thoả `LakehouseResolver`, và một `FakeAuthz` tiêm
    # qua `authz=` (`tests/conftest.py`) cũng cố tình cài cả hai để test không
    # phải tiêm hai đối tượng giả cho một thứ về bản chất là "hỏi loom-api". Ép
    # kiểu ở đây (`cast`) là trung thực với NHỮNG GÌ hai class đó THẬT SỰ làm,
    # không phải một cách né mypy.
    resolved_resolver: LakehouseResolver = resolver or cast(LakehouseResolver, resolved_authz)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_http and http is not None:
            await http.aclose()

    app = FastAPI(title="Loom Query", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.authz = resolved_authz
    app.state.resolver = resolved_resolver
    app.state.store = store
    app.state.storage = resolved_storage
    # KHÔNG dependencies bí mật chia sẻ — probe của kubelet, xem docstring
    # `routers/health.py`.
    app.include_router(health.router)
    app.include_router(query.router, prefix="/api/v1")
    app.include_router(lakehouses.router, prefix="/api/v1")
    return app
