import logging

import structlog
from httpx import AsyncClient

from loom_api.main import create_app
from loom_api.middleware import RequestContextMiddleware


async def test_response_carries_request_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/healthz")
    assert response.headers["x-request-id"]


async def test_incoming_request_id_is_preserved(client: AsyncClient) -> None:
    response = await client.get("/api/v1/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


async def test_each_request_gets_distinct_id(client: AsyncClient) -> None:
    first = await client.get("/api/v1/healthz")
    second = await client.get("/api/v1/healthz")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_request_state_carries_the_same_id_as_the_header() -> None:
    """Mọi store đọc `request.state.request_id` để ghi vào `audit_log.request_id`,
    và chúng đọc bằng `getattr(..., "-")`. Nên thiếu nó là một lỗi IM LẶNG: audit
    ghi toàn dấu gạch, không có ngoại lệ nào, và bảng audit lẫn log cùng tồn tại mà
    không ghép được với nhau — đúng cái mà cột đó sinh ra để làm.

    Bind vào contextvars KHÔNG thay được điều này, nên test về log context ở dưới
    không nhìn thấy thuộc tính này. Đây là chỗ duy nhất canh nó.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"from_state": getattr(request.state, "request_id", None)})

    app = Starlette(routes=[Route("/echo", echo)])
    app.add_middleware(RequestContextMiddleware)

    from httpx import ASGITransport

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/echo", headers={"X-Request-ID": "gan-vao-state"})

    assert r.json()["from_state"] == "gan-vao-state"
    assert r.headers["x-request-id"] == "gan-vao-state"


async def test_log_context_contains_request_id(client: AsyncClient) -> None:
    captured: list[dict[str, object]] = []

    def capture(_logger: object, _name: str, event_dict: dict[str, object]) -> str:
        captured.append(dict(event_dict))
        # BẮT BUỘC trả về str, không phải dict — PrintLogger.msg() chỉ nhận chuỗi
        return ""

    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, capture],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    await client.get("/api/v1/healthz", headers={"X-Request-ID": "log-me"})
    assert any(entry.get("request_id") == "log-me" for entry in captured)


async def test_context_does_not_leak_across_requests(client: AsyncClient) -> None:
    """Một request trước bind thêm contextvar (ví dụ workspace_id do resolver auth
    ở Task 6-8 đặt) không được rò sang log của request sau."""
    captured: list[dict[str, object]] = []

    def capture(_logger: object, _name: str, event_dict: dict[str, object]) -> str:
        captured.append(dict(event_dict))
        return ""

    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, capture],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )

    structlog.contextvars.bind_contextvars(workspace_id="ws-should-not-leak")
    await client.get("/api/v1/healthz")

    captured.clear()
    await client.get("/api/v1/healthz")

    leaked = [entry for entry in captured if "workspace_id" in entry]
    assert not leaked, f"workspace_id rò sang log của request sau: {leaked}"


async def test_request_context_middleware_is_outermost() -> None:
    """Canh bất biến thứ tự đăng ký — index 0 nghĩa là ngoài cùng."""
    app = create_app()
    # Chạy lifespan dù test này không gửi request nào: quy tắc là **mọi**
    # create_app() phải có lifespan tương ứng, nếu không engine bị bỏ rơi.
    async with app.router.lifespan_context(app):
        assert app.user_middleware[0].cls is RequestContextMiddleware
