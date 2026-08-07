"""`/internal/*` không tới được từ bên ngoài cluster — canh bằng CẤU TRÚC route,
không bằng một mã trạng thái xác thực nào cả.

`loom-query` hỏi quyền qua `POST /internal/authz/items`
(`loom_api.routers.internal`), và endpoint đó CỐ Ý không có `PrincipalDep`:
người gọi là một pod khác trong cùng cluster, chuyển tiếp principal của người
dùng cuối trong thân yêu cầu — không phải trình duyệt mang cookie phiên. Không
có dependency HTTP nào bảo vệ nó. Thứ bảo vệ duy nhất là ingress không định
tuyến traffic từ ngoài cluster tới nó.

`deploy/helm/loom/templates/ingress.yaml` định tuyến ĐÚNG hai path: `/api` (tới
service `-api`, tức pod chạy app này) và `/` (tới service `-web`, một pod khác
hẳn). Router `internal` gắn với prefix `/internal` (xem `main.py`), KHÔNG
`/api/v1` như mọi router khác — nên một request `/internal/authz/items` từ bên
ngoài khớp rule `/` và đi tới web, không bao giờ chạm pod này.

Bất biến cần giữ: KHÔNG route "nội bộ" nào (path mang `/internal`) được đăng ký
dưới một prefix mà ingress CÓ chuyển tới service API. Nếu có, request từ ngoài
cluster chạm thẳng `/internal/authz/items` và bỏ qua toàn bộ RBAC — đúng thứ
router đó tồn tại để tránh.

`API_INGRESS_PREFIX` viết THẲNG ra dưới dạng literal, không suy ra bằng cách tự
parse `ingress.yaml` lúc chạy test: file đó là YAML pha cú pháp Helm
(`{{- if ... }}`), một parser tự chế cho nó dễ đọc sai hơn là đáng tin — cùng lý
do `PUBLIC_API_PATHS` ở `test_route_auth.py` là literal chứ không tự sinh.
`test_ingress_still_only_serves_these_two_paths` bên dưới là chốt-chống-lệch:
nó đọc NGUYÊN VĂN file thật và báo đỏ nếu ai thêm một `path:` thứ ba mà không
ghé qua hằng số ở đây.
"""

from pathlib import Path

from fastapi.routing import iter_route_contexts

from loom_api.main import create_app

# Từ `deploy/helm/loom/templates/ingress.yaml`: MỌI request bên ngoài chạm tới
# loom-api chỉ qua path này. Path `/` route tới service `-web`, một pod khác —
# route nào KHÔNG nằm dưới `/api` thì không có đường nào từ ngoài cluster tới
# service API, bất kể path đó là gì.
API_INGRESS_PREFIX = "/api"

INGRESS_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "deploy" / "helm" / "loom" / "templates" / "ingress.yaml"
)


def _all_route_paths() -> list[str]:
    app = create_app()
    return [context.path for context in iter_route_contexts(app.routes) if context.path]


def test_the_walk_actually_finds_both_kinds_of_route() -> None:
    """Chốt chống-xanh-rỗng, cùng tinh thần `test_the_walk_actually_finds_the_routes`
    ở `test_route_auth.py`: nếu phép duyệt route đổi cách hoạt động và trả về
    danh sách rỗng, câu khẳng định chính bên dưới sẽ đúng một cách vô nghĩa."""
    paths = _all_route_paths()
    assert any(p.startswith("/internal") for p in paths), paths
    assert any(p.startswith(API_INGRESS_PREFIX) for p in paths), paths


def test_no_internal_route_sits_under_the_ingress_served_prefix() -> None:
    """Phép canh CHÍNH. Đỏ khi router `internal` (hay bất cứ route nào mang
    `/internal`) bị gắn nhầm dưới `/api/v1` thay vì `/internal` — tức là đúng
    lúc nó đổi từ "không thể tới được" sang "tới được từ bên ngoài cluster"."""
    offending = sorted(
        p for p in _all_route_paths() if p.startswith(API_INGRESS_PREFIX) and "/internal" in p
    )
    assert not offending, (
        f"Route sau đây nằm DƯỚI prefix `{API_INGRESS_PREFIX}` mà ingress chuyển tới "
        f"service API, nhưng path lại mang '/internal': {offending}. Ingress không lọc "
        "theo tên — bất kỳ path nào dưới `/api` đều tới thẳng pod này từ bên ngoài "
        "cluster, kể cả path trông có vẻ nội bộ. Gắn router này với prefix `/internal`, "
        "không phải một prefix con của `/api`."
    )


def test_ingress_still_only_serves_these_two_paths() -> None:
    """Nếu ai thêm một `path:` thứ ba vào ingress.yaml, `API_INGRESS_PREFIX` ở
    trên có thể không còn nói hết sự thật — test này buộc người đó ghé qua đây
    thay vì để phép canh chính âm thầm bỏ sót một đường mới."""
    text = INGRESS_TEMPLATE.read_text()
    paths = [line.split("path:", 1)[1].strip() for line in text.splitlines() if "path:" in line]
    assert paths == ["/api", "/"], (
        f"ingress.yaml giờ định tuyến {paths}, khác giả định [/api, /] mà "
        "test_internal_route_boundary.py dựa vào. Cập nhật API_INGRESS_PREFIX và các "
        "câu khẳng định ở trên cho khớp trước khi tin phép canh này."
    )
