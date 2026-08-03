import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_LENGTH = 128

# Probe của Kubernetes gõ vào đây mỗi 10-20 giây, mỗi replica, mãi mãi.
# Hạ xuống DEBUG: vẫn bật được khi cần chẩn đoán, nhưng không đổ vào Loki.
_QUIET_PATHS = frozenset({"/api/v1/healthz", "/api/v1/readyz"})

logger = structlog.get_logger(__name__)


class RequestContextMiddleware:
    """Gắn request_id vào contextvars để mọi log trong request đều mang nó.

    ASGI thuần chứ không phải BaseHTTPMiddleware: BaseHTTPMiddleware chạy app
    phía dưới trong một task anyio riêng, nên contextvar bind bên trong endpoint
    không nhìn thấy được từ đây sau khi request kết thúc — đúng những trường
    (workspace_id, run_id) mà dòng request.finished cần mang.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        incoming = request.headers.get(REQUEST_ID_HEADER, "")[:MAX_REQUEST_ID_LENGTH]
        request_id = incoming or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        log = logger.debug if request.url.path in _QUIET_PATHS else logger.info
        log("request.started")

        status_code: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("request.failed")
            raise

        log("request.finished", status_code=status_code)
