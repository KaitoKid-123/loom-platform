from fastapi import APIRouter, Request, Response
from fastapi import status as http_status
from sqlalchemy import text

from loom_api import VERSION
from loom_core.schemas import HealthStatus, ReadyStatus

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    """Liveness — chỉ trả lời được là process còn sống."""
    return HealthStatus(status="ok", version=VERSION)


@router.get("/readyz", response_model=ReadyStatus)
async def readyz(request: Request, response: Response) -> ReadyStatus:
    """Readiness — chỉ 'ok' khi thực sự phục vụ được request."""
    checks: dict[str, str] = {}
    try:
        async with request.app.state.db.session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # mọi lỗi đều là not-ready (BLE001 không nằm trong ruleset dự án)
        checks["database"] = f"error: {type(exc).__name__}"

    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyStatus(status="ok" if ready else "degraded", checks=checks)
