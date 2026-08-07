"""SELECT đơn giản qua HTTP, trên một bảng Iceberg THẬT — MinIO + Postgres +
Lakekeeper thật (xem `conftest.py`), không một phần nào bị giả lập ngoài
`AuthzPort` (đã có differential test riêng bên `loom-api`; xem
`tests/conftest.py::FakeAuthz`).

Đây là bằng chứng cho mục 4 của phạm vi Task 6: `loom-query` CHẠY ĐƯỢC một câu
SELECT thật, không chỉ có cổng quyền đúng trên giấy.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from loom_core.schemas import Principal
from loom_query.config import Settings
from loom_query.main import create_app

from ..conftest import FakeAuthz

# `fake_authz`/`principal` (tests/conftest.py) và `app_settings`/
# `seeded_table`/`lakehouse_id` (tests/integration/conftest.py) là fixture,
# tiêm thẳng theo tên tham số — không cần import. `FakeAuthz` ở trên chỉ để
# chú thích kiểu.

pytestmark = pytest.mark.integration


def _body(lakehouse_id: uuid.UUID, sql: str, principal: Principal) -> dict[str, Any]:
    return {
        "lakehouse_id": str(lakehouse_id),
        # Mọi câu SQL ở file này dùng bảng HAI phần — `run_gate` không hỏi gì
        # về workspace cho trường hợp đó, nên một UUID ngẫu nhiên là đủ.
        "workspace_id": str(uuid.uuid4()),
        "sql": sql,
        "principal": {
            "user_id": str(principal.user_id),
            "subject": principal.subject,
            "email": principal.email,
            "display_name": principal.display_name,
            "groups": list(principal.groups),
        },
    }


async def test_select_over_http_returns_rows_from_a_real_iceberg_table(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    assert seeded_table == "sales.orders"
    fake_authz.grant(lakehouse_id, "viewer")

    app = create_app(settings=app_settings, authz=fake_authz)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/query",
            json=_body(lakehouse_id, "SELECT id, amount FROM sales.orders ORDER BY id", principal),
        )
        assert create_response.status_code == 202, create_response.text
        query_id = create_response.json()["query_id"]

        body: dict[str, Any] = {}
        # Poll tới khi xong, KHÔNG sleep cố định — cùng lý do với
        # `_wait_for_health` ở conftest.py: một sleep cố định luôn sai theo
        # một hướng (thừa trên máy nhanh, thiếu trên máy chậm/CI tải cao).
        for _ in range(200):
            status_response = await client.get(f"/api/v1/query/{query_id}")
            body = status_response.json()
            if body["status"] != "running":
                break
            await asyncio.sleep(0.05)

    assert body["status"] == "succeeded", body
    assert [c["name"] for c in body["columns"]] == ["id", "amount"]
    assert body["rows"] == [[1, 10.0], [2, 20.0], [3, 30.0]]


async def test_a_table_outside_the_granted_lakehouse_is_forbidden(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """KHÔNG `grant` gì trên `lakehouse_id` — dù bảng THẬT tồn tại và catalog
    THẬT với tới được, câu trả lời vẫn phải là 403, và phải tới NGAY trong
    response của chính `POST` (không cần polling `GET`)."""
    app = create_app(settings=app_settings, authz=fake_authz)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/query",
            json=_body(lakehouse_id, "SELECT id, amount FROM sales.orders", principal),
        )

    assert response.status_code == 403
