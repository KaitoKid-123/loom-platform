"""`GET /healthz` — probe của Kubernetes (Task 10/11), KHÔNG qua cổng bí mật
chia sẻ.

`kubelet` không biết, và không nên biết, bí mật chia sẻ với `loom-api` (xem
`security.require_shared_secret`) — nó chỉ cần biết tiến trình còn sống để
quyết định restart/định tuyến traffic. Route này vì vậy đứng ở một
`APIRouter` RIÊNG, không mang `dependencies=[Depends(require_shared_secret)]`
như `routers/query.py` — trộn chung một router thì hoặc probe luôn 401 (pod
không bao giờ Ready), hoặc ai đó "sửa" bằng cách gỡ dependency khỏi CẢ router,
mở toang ba route nghiệp vụ.

Không lộ gì có giá trị cho một request giả mạo: không lakehouse, không SQL,
không principal — cùng tinh thần `loom_api.routers.health`.
"""

from fastapi import APIRouter

from loom_core.schemas import HealthStatus
from loom_query import VERSION

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus(status="ok", version=VERSION)
