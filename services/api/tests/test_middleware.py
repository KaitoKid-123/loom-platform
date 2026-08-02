import structlog
from httpx import AsyncClient


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


async def test_log_context_contains_request_id(client: AsyncClient) -> None:
    captured: list[dict[str, object]] = []

    def capture(_logger: object, _name: str, event_dict: dict[str, object]) -> str:
        captured.append(dict(event_dict))
        # BẮT BUỘC trả về str, không phải dict — PrintLogger.msg() chỉ nhận chuỗi
        return ""

    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, capture],
    )
    await client.get("/api/v1/healthz", headers={"X-Request-ID": "log-me"})
    assert any(entry.get("request_id") == "log-me" for entry in captured)
