"""Giới hạn 3 (Task 8) — trần dòng trả về, với cờ `truncated` nói rõ đã cắt.

**Chứng minh đỏ 3 của Task 8** (bắt buộc): sửa `test_a_small_cap_truncates_and_
says_so` bên dưới, đổi `max_result_rows` từ `2` thành một số cực lớn (ví dụ
`1_000_000`) rồi chạy lại. Phép kiểm phải ĐỎ ở khẳng định `truncated is True`
(với `max_result_rows` vô hạn hiệu quả, ba dòng của `sales.orders` không còn bị
cắt) — xem log chạy tay trong báo cáo hoàn tất task; bản commit giữ nguyên `2`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from loom_core.schemas import Principal
from loom_query.config import Settings
from loom_query.main import create_app

from ..conftest import FakeAuthz, http_client

# `fake_authz`/`principal`/`app_settings`/`seeded_table`/`lakehouse_id` là
# fixture, tiêm thẳng theo tên tham số — không cần import.

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


async def _run_and_wait(
    app: FastAPI, lakehouse_id: uuid.UUID, sql: str, principal: Principal
) -> dict[str, Any]:
    async with http_client(app) as client:
        create_response = await client.post(
            "/api/v1/query", json=_body(lakehouse_id, sql, principal)
        )
        assert create_response.status_code == 202, create_response.text
        query_id = create_response.json()["query_id"]

        body: dict[str, Any] = {}
        for _ in range(200):
            status_response = await client.get(f"/api/v1/query/{query_id}")
            body = status_response.json()
            if body["status"] != "running":
                break
            await asyncio.sleep(0.05)
    return body


async def test_a_small_cap_truncates_and_says_so(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """`sales.orders` có 3 dòng thật; trần đặt = 2 để phép cắt tất định mà
    không cần sinh dữ liệu lớn."""
    fake_authz.grant(lakehouse_id, "viewer")
    capped = app_settings.model_copy(update={"max_result_rows": 2})
    app = create_app(settings=capped, authz=fake_authz)

    body = await _run_and_wait(
        app, lakehouse_id, "SELECT id, amount FROM sales.orders ORDER BY id", principal
    )

    assert body["status"] == "succeeded", body
    assert body["truncated"] is True
    assert body["row_count"] == 3
    assert body["rows"] == [[1, 10.0], [2, 20.0]]


async def test_a_generous_cap_does_not_truncate(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    fake_authz.grant(lakehouse_id, "viewer")
    # Cap mặc định (10.000) — ba dòng thật lọt xa dưới trần.
    app = create_app(settings=app_settings, authz=fake_authz)

    body = await _run_and_wait(
        app, lakehouse_id, "SELECT id, amount FROM sales.orders ORDER BY id", principal
    )

    assert body["status"] == "succeeded", body
    assert body["truncated"] is False
    assert body["row_count"] == 3
    assert body["rows"] == [[1, 10.0], [2, 20.0], [3, 30.0]]
