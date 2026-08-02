from fastapi import APIRouter

from loom_core.schemas import HealthStatus

VERSION = "0.1.0"

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    """Liveness — chỉ trả lời được là process còn sống."""
    return HealthStatus(status="ok", version=VERSION)
