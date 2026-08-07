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
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import SessionDep
from loom_api.permissions import PermissionService
from loom_core.schemas import AuthzItemsRequest, AuthzItemsResponse

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
