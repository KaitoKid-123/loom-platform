"""`POST /internal/authz/items` — điểm hỏi quyền cho `loom-query`.

`loom-query` chạy SQL trên bảng Iceberg và cần biết principal gọi nó có đọc
được những item mà câu SQL chạm tới. Nó KHÔNG tự tính quyền: Giai đoạn 1 đã gom
luật vào MỘT nguồn (`permissions.py`) và giữ nó đúng bằng một differential test
canh hai đường đánh giá (một-tài-nguyên và lọc-danh-sách) không trôi khỏi nhau.
Thêm một đường thứ ba tính lại luật đó bên trong `loom-query` là phá đúng thứ
differential test kia tồn tại để bảo vệ. Endpoint này là đường DUY NHẤT mà
`loom-query` được phép hỏi, và nó chỉ dịch câu hỏi thành lời gọi tới đúng những
hàm quyền đã có — không thêm điều kiện nào của riêng nó.

Router này KHÔNG có `PrincipalDep`, và đó là cố ý chứ không phải sót. Người gọi
không phải trình duyệt mang cookie phiên (`get_principal` đọc cookie
`loom_session` — xem `deps.py`); người gọi là `loom-query`, một pod khác trong
cùng cluster, CHUYỂN TIẾP principal của người dùng cuối trong thân yêu cầu.
Không có phiên nào để đọc ở đây.

Bảo vệ vì vậy nằm ở tầng MẠNG, không phải tầng HTTP: router này gắn với prefix
`/internal` (xem `main.py`), KHÔNG `/api/v1` như mọi router khác, và
`deploy/helm/loom/templates/ingress.yaml` chỉ chuyển tới service API đúng một
path — `/api`. Request `/internal/authz/items` từ bên ngoài cluster khớp rule
`/` của ingress và đi tới service web, không bao giờ chạm pod này.
`tests/test_internal_route_boundary.py` khẳng định đúng cấu trúc đó và tự chứng
minh nó đỏ được khi router bị gắn nhầm dưới `/api/v1`.

Không rò rỉ sự tồn tại: `null` cho item không tồn tại và `null` cho item người
gọi không thấy phải giống hệt nhau ở phía gọi — cùng mã trạng thái (200), cùng
hình dạng phản hồi. `effective_roles_for_items` đã giữ bất biến đó (xem docstring
của nó trong `permissions.py`); handler ở đây không được thêm bất kỳ nhánh nào
kiểu "item không tồn tại thì trả khác đi", vì đó đúng là lỗ 404-trước-403 của
Giai đoạn 1 sinh ra để chặn, chỉ đổi hướng.
"""

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import SessionDep
from loom_api.models import ACTIVE, Item
from loom_api.permissions import PermissionService
from loom_core.item_definitions import ItemType
from loom_core.schemas import (
    AuthzItemsRequest,
    AuthzItemsResponse,
    LakehouseResolveRequest,
    LakehouseResolveResponse,
)

router = APIRouter(tags=["internal"])


@router.post("/authz/items", response_model=AuthzItemsResponse)
async def authz_items(
    body: AuthzItemsRequest,
    session: AsyncSession = SessionDep,
) -> AuthzItemsResponse:
    perms = PermissionService(session, body.principal)
    roles = await perms.effective_roles_for_items(body.item_ids)
    return AuthzItemsResponse(
        roles={
            str(item_id): (str(role) if role is not None else None)
            for item_id, role in roles.items()
        }
    )


@router.post("/lakehouses/resolve", response_model=LakehouseResolveResponse)
async def resolve_lakehouses(
    body: LakehouseResolveRequest,
    session: AsyncSession = SessionDep,
) -> LakehouseResolveResponse:
    """`name` của item `type='lakehouse'` -> `id`, cho tên bảng BA PHẦN
    (`lakehouse.namespace.table`) mà `loom-query` cần phân giải trước khi hỏi
    quyền — xem docstring `loom_query.authz._resolve_item_ids`.

    **KHÔNG kiểm quyền ở đây, và đó là cố ý, không phải sót.** Endpoint này chỉ
    dịch một cái TÊN (thứ người dùng gõ trong SQL) sang một item id; câu hỏi
    "principal này có ĐỌC được lakehouse đó không" là việc của
    `/internal/authz/items`, và `loom-query` LUÔN gọi endpoint đó ngay sau đây
    với id vừa phân giải (xem `run_gate`). Tách hai câu hỏi ra hai endpoint là
    để mỗi cái làm ĐÚNG MỘT việc — trộn chung sẽ mời một điều kiện quyền thứ
    hai lặng lẽ trôi khỏi `PermissionService`, đúng thứ differential test của
    `permissions.py` sinh ra để chặn (xem docstring module đó).

    Endpoint này trả `null` cho một tên KHÔNG TỒN TẠI giống hệt một tên tồn
    tại nhưng principal không có quyền đọc — vì bước kiểm quyền diễn ra ở
    request THỨ HAI (`/internal/authz/items`), không phải ở đây, "tồn tại hay
    không" tạm thời không phân biệt được ở BƯỚC NÀY dù chưa biết principal là
    ai. Điều đó nghe như một lỗ rò rỉ sự tồn tại, nhưng KHÔNG PHẢI: nó rò rỉ
    (nếu có) *cho `loom-query`*, một pod nội bộ, không phải cho người dùng
    cuối — `run_gate` biến MỌI thất bại phân giải (tên sai HAY không có
    quyền) thành cùng một 403 trước khi trả lời bất kỳ ai ở ngoài. Đừng "sửa"
    chỗ này thành hai nhánh phản hồi khác nhau; làm vậy chỉ chuyển lỗ rò rỉ từ
    chỗ vô hại (nội bộ cluster) sang chỗ có hại (ra tới người dùng), vì
    `loom-query` sẽ phải LỘ lại phân biệt đó để giữ hành vi của chính nó.

    **MỘT truy vấn cho toàn bộ `names`** — `Item.name.in_(...)`, không lặp
    từng tên: một câu `JOIN` chạm năm lakehouse không được sinh năm round trip.

    Hai điều kiện lọc BẮT BUỘC, cả hai đã kiểm trực tiếp trên
    `uq_item_active_name`/migration `0003` (`UNIQUE (workspace_id, type, name)
    WHERE state = 'active'`):

    - `Item.type == lakehouse`: `type` nằm TRONG ràng buộc duy nhất, nên một
      `sql_script` và một `lakehouse` cùng tên cùng tồn tại được trong một
      workspace. Thiếu điều kiện này, một tên khớp nhiều hàng và kết quả phụ
      thuộc thứ tự Postgres trả về — có thể phân giải nhầm sang item KHÔNG
      phải lakehouse.
    - `Item.state == ACTIVE`: ràng buộc chỉ là PARTIAL trên `state = 'active'`,
      nên một lakehouse đã xoá mềm không chặn một lakehouse MỚI mang cùng tên
      được tạo lại. Thiếu điều kiện này, phân giải có thể trả về id của bản đã
      xoá thay vì bản đang sống — hoặc trả kết quả không xác định nếu cả hai
      hàng cùng khớp `name`.
    """
    if not body.names:
        return LakehouseResolveResponse(ids={})

    stmt = select(Item.name, Item.id).where(
        Item.workspace_id == body.workspace_id,
        Item.type == str(ItemType.lakehouse),
        Item.state == ACTIVE,
        Item.name.in_(body.names),
    )
    found = {name: item_id for name, item_id in (await session.execute(stmt)).all()}
    return LakehouseResolveResponse(ids={name: found.get(name) for name in body.names})
