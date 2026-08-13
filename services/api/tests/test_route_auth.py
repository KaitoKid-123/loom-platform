"""Không endpoint nào của /api/v1 được phép công khai ngoài allowlist.

Task 4 gom việc xác thực vào MỘT dependency để không ai quên nó. Nhưng "không
ai quên" vẫn chỉ là một quy ước cho tới khi có thứ kiểm nó: một handler kiểu
Giai đoạn 1b thêm vào mà thiếu `PrincipalDep` đi qua được cả bộ test lẫn ruff và
mypy, trong khi ai cũng gọi được nó mà không cần đăng nhập. Task 2 có quy tắc
tương tự (`roles.py` không được biết tầng lưu trữ) và nó được khoá bằng một test
đọc AST — đây là bản tương đương cho tầng HTTP.

Phép kiểm đi theo CÂY dependency chứ không chỉ nhìn chữ ký hàm, nên các
dependency của Giai đoạn 1b xếp CHỒNG lên get_principal (`require_permission`,
`workspace_member`...) vẫn được tính là đã xác thực mà không phải sửa gì ở đây.
"""

import pytest
from fastapi.routing import iter_route_contexts
from httpx import ASGITransport, AsyncClient

from loom_api.deps import get_principal
from loom_api.main import create_app
from loom_core.config import get_settings

# Allowlist viết thẳng ra dưới dạng literal, KHÔNG suy ra từ app: thêm một
# đường dẫn vào đây phải là một hành động nhìn thấy được trong diff và review
# được. Một allowlist tự sinh sẽ hợp thức hoá đúng cái lỗi mà test này tồn tại
# để bắt.
#
# healthz/readyz: kubelet gọi, không có cookie.
# auth/login|callback|logout: chính là đường để CÓ một phiên.
# docs|openapi.json: CHỈ tồn tại ở local (xem test_api_docs_are_local_only).
#   Ở dev/prod chúng không được đăng ký nên không có route nào để canh.
PUBLIC_API_PATHS = frozenset(
    {
        "/api/v1/healthz",
        "/api/v1/readyz",
        "/api/v1/auth/login",
        "/api/v1/auth/callback",
        "/api/v1/auth/logout",
        "/api/v1/docs",
        "/api/v1/openapi.json",
    }
)

API_PREFIX = "/api/v1"


def _dependency_calls(dependant: object) -> set[object]:
    """Mọi callable trong cây dependency của một route, kể cả cấp lồng nhau."""
    found: set[object] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        call = getattr(node, "call", None)
        if call is not None:
            found.add(call)
        stack.extend(getattr(node, "dependencies", []))
    return found


def _api_routes() -> list[tuple[str, tuple[str, ...], set[object]]]:
    """(path, methods, callables trong cây dependency) cho mọi route /api/v1.

    `iter_route_contexts` là hàm FastAPI dùng cho chính việc sinh OpenAPI; nó
    làm phẳng `_IncludedRouter` và áp prefix của `include_router`, nên
    `app.routes` trần (chỉ có bốn route gốc cộng hai router chưa mở) không đủ.
    """
    app = create_app()
    routes = []
    for context in iter_route_contexts(app.routes):
        path = context.path
        if not path or not path.startswith(API_PREFIX):
            continue
        methods = tuple(sorted(context.methods or ()))
        routes.append((path, methods, _dependency_calls(getattr(context, "dependant", None))))
    return routes


def test_the_walk_actually_finds_the_routes() -> None:
    """Chốt chống-xanh-rỗng. Nếu FastAPI đổi cách tổ chức route và phép duyệt
    trả về danh sách rỗng, mọi assert bên dưới sẽ đúng một cách vô nghĩa và
    canh gác biến mất trong im lặng — đúng kiểu hỏng mà một test canh gác không
    bao giờ được phép có."""
    paths = {path for path, _methods, _deps in _api_routes()}
    assert "/api/v1/me" in paths, paths
    assert paths >= PUBLIC_API_PATHS, sorted(PUBLIC_API_PATHS - paths)


def test_every_api_route_requires_authentication() -> None:
    """Endpoint mới mặc định là RIÊNG TƯ. Quên `PrincipalDep` là một endpoint
    công khai, và không có test này thì không gì báo — bộ test xanh, lint sạch,
    dữ liệu ra ngoài cho khách vãng lai."""
    unprotected = sorted(
        f"{','.join(methods) or 'ANY'} {path}"
        for path, methods, deps in _api_routes()
        if path not in PUBLIC_API_PATHS and get_principal not in deps
    )
    assert not unprotected, (
        f"Route dưới {API_PREFIX} không có get_principal trong cây dependency: "
        f"{unprotected}. Thêm `PrincipalDep` (hoặc một dependency phân quyền dựng "
        f"trên nó) vào handler. Nếu endpoint này THẬT SỰ phải công khai thì thêm "
        f"đường dẫn vào PUBLIC_API_PATHS trong test này — một dòng, và người review "
        f"nhìn thấy nó."
    )


def test_the_allowlist_describes_reality() -> None:
    """Allowlist phải nói đúng hiện trạng, không được thừa. Một đường dẫn đã
    được bảo vệ mà vẫn nằm trong allowlist là một tấm chắn đang mở sẵn cho lần
    đổi tên route tiếp theo tình cờ rơi trúng cái tên đó.

    `/api/v1/docs` và `/api/v1/openapi.json` không xác thực từ Giai đoạn 0 và
    do đó công bố toàn bộ bề mặt API cho mọi người. Chúng nằm đây để test này
    phản ánh đúng sự thật hôm nay, KHÔNG phải vì hiện trạng đó là đúng."""
    still_public = sorted(
        path
        for path, _methods, deps in _api_routes()
        if path in PUBLIC_API_PATHS and get_principal in deps
    )
    assert not still_public, f"{still_public} đã yêu cầu xác thực — bỏ khỏi PUBLIC_API_PATHS."


@pytest.mark.parametrize(
    ("environment", "expect_status"),
    [("local", 200), ("dev", 404), ("prod", 404)],
)
async def test_api_docs_are_local_only(
    monkeypatch: pytest.MonkeyPatch, environment: str, expect_status: int
) -> None:
    """Từ Giai đoạn 1b, bề mặt API CHÍNH LÀ mô hình RBAC — mọi đường
    workspace/item/role/audit, tên tham số, hình dạng mọi response. Với người
    chưa đăng nhập đó là một bản đồ trinh sát miễn phí.

    Đóng bằng cách không đăng ký route (404), chứ không phải đặt sau xác thực
    (401): một route không tồn tại thì không có bề mặt nào để dò.
    """
    monkeypatch.setenv("LOOM_ENVIRONMENT", environment)
    if environment != "local":
        # Settings từ chối secret mặc định ngoài local, nên phải cấp giá trị thật.
        monkeypatch.setenv("LOOM_SESSION_SECRET", "x" * 48)
        monkeypatch.setenv("LOOM_OIDC_CLIENT_SECRET", "y" * 48)
        monkeypatch.setenv("LOOM_QUERY_SHARED_SECRET", "z" * 48)
        monkeypatch.setenv("LOOM_INGEST_SHARED_SECRET", "v" * 48)
        monkeypatch.setenv("LOOM_STORAGE_ROOT_SECRET_KEY", "w" * 48)
    get_settings.cache_clear()

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/api/v1/docs", "/api/v1/openapi.json"):
                assert (await client.get(path)).status_code == expect_status, path
