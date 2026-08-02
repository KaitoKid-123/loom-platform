from fastapi import APIRouter

from loom_api import VERSION
from loom_core.schemas import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    """Liveness — chỉ trả lời được là process còn sống."""
    return HealthStatus(status="ok", version=VERSION)
