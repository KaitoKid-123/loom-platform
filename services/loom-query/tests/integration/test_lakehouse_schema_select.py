"""`GET /api/v1/lakehouses/{lakehouse_id}/schema` trên một bảng Iceberg THẬT —
MinIO + Postgres + Lakekeeper thật (xem `conftest.py`), không phần nào bị giả
lập ngoài `AuthzPort` (đã có differential test riêng bên `loom-api`, xem
`tests/conftest.py::FakeAuthz`).

Đây là VẾ KHẲNG ĐỊNH bắt buộc của Task 2 (Giai đoạn 2c): không có nó, một bản
cài từ chối MỌI request cũng làm chứng minh đỏ 1/2 xanh — xem cảnh báo ở đầu
module docstring `authz.py` cho đúng lỗi này đã từng xảy ra ở Task 6.

Kiểm CẢ kiểu cột, không chỉ tên: autocomplete gợi đúng tên nhưng sai kiểu vẫn
dẫn người dùng viết một câu SQL không chạy (spec Giai đoạn 2c, Task 2).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from loom_core.schemas import Principal
from loom_query.config import Settings
from loom_query.main import create_app

from ..conftest import FakeAuthz, http_client

# `fake_authz`/`principal` (tests/conftest.py) và `app_settings`/
# `seeded_table`/`lakehouse_id` (tests/integration/conftest.py) là fixture,
# tiêm thẳng theo tên tham số — không cần import. `FakeAuthz` ở trên chỉ để
# chú thích kiểu.

pytestmark = pytest.mark.integration


def _principal_json(principal: Principal) -> dict[str, Any]:
    return {
        "user_id": str(principal.user_id),
        "subject": principal.subject,
        "email": principal.email,
        "display_name": principal.display_name,
        "groups": list(principal.groups),
    }


async def test_columns_depth_returns_real_namespace_table_and_column_types(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """`seeded_table` (`tests/integration/conftest.py`) là `sales.orders`, hai
    cột THẬT: `id` (int64) và `amount` (float64) — kiểu tra được TRỰC TIẾP
    trên `pa.Schema` đã ghi khi tạo bảng, không phải suy đoán."""
    assert seeded_table == "sales.orders"
    fake_authz.grant(lakehouse_id, "viewer")

    app = create_app(settings=app_settings, authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=columns",
            json={"principal": _principal_json(principal)},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "namespaces": [
            {
                "name": "sales",
                "tables": [
                    {
                        "name": "orders",
                        "columns": [
                            {"name": "id", "type": "int64"},
                            {"name": "amount", "type": "double"},
                        ],
                    }
                ],
            }
        ]
    }


async def test_default_depth_lists_the_real_table_name_without_columns(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    fake_authz.grant(lakehouse_id, "viewer")

    app = create_app(settings=app_settings, authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema",
            json={"principal": _principal_json(principal)},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"namespaces": [{"name": "sales", "tables": [{"name": "orders"}]}]}


async def test_a_lakehouse_without_the_granted_permission_is_forbidden(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """KHÔNG `grant` gì — dù bảng THẬT tồn tại và catalog THẬT với tới được,
    câu trả lời vẫn phải là 403 (cùng cấu trúc `test_query_select.py::
    test_a_table_outside_the_granted_lakehouse_is_forbidden`)."""
    app = create_app(settings=app_settings, authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=columns",
            json={"principal": _principal_json(principal)},
        )

    assert response.status_code == 403
