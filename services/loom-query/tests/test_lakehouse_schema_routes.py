"""Test HTTP của `GET /api/v1/lakehouses/{lakehouse_id}/schema`, KHÔNG cần
Docker — `lakehouse_schema.build_schema_tree` bị thay bằng bản giả
(monkeypatch), cùng cách `test_query_routes.py::
test_allowed_query_reaches_the_background_runner` giả `runner.execute`: các
phép kiểm ở đây khẳng định cổng quyền + route + tham số `depth` nối đúng với
nhau, KHÔNG khẳng định PyIceberg đọc đúng cột thật — đó là việc của
`tests/integration/test_lakehouse_schema_select.py` (cần Lakekeeper thật).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from loom_core.schemas import Principal
from loom_query.main import create_app
from loom_query.schemas import ColumnOut, LakehouseSchemaOut, NamespaceSchemaOut, TableSchemaOut

from .conftest import FakeAuthz, http_client

# `fake_authz`/`principal` là fixture của `tests/conftest.py`, tiêm thẳng theo
# tên tham số — không cần import. `FakeAuthz` ở trên chỉ để chú thích kiểu.


def _principal_json(principal: Principal) -> dict[str, Any]:
    return {
        "user_id": str(principal.user_id),
        "subject": principal.subject,
        "email": principal.email,
        "display_name": principal.display_name,
        "groups": list(principal.groups),
    }


async def test_missing_role_is_403_and_never_builds_the_tree(
    fake_authz: FakeAuthz, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bằng chứng cổng quyền chạy TRƯỚC khi chạm catalog: nếu `build_schema_tree`
    bị gọi ở đây, `AssertionError` dưới đây làm bài này đỏ vì lý do SAI (lộ ra
    ngay, không mập mờ) thay vì lặng lẽ trả 403 vì một catalog không thể mở
    tới."""

    def fail_if_called(*_args: object, **_kwargs: object) -> LakehouseSchemaOut:
        raise AssertionError("build_schema_tree should never run without a viewer role")

    monkeypatch.setattr("loom_query.routers.lakehouses.build_schema_tree", fail_if_called)

    lakehouse_id = uuid.uuid4()
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema",
            json={"principal": _principal_json(principal)},
        )

    assert response.status_code == 403


async def test_default_depth_is_tables_and_omits_columns(
    fake_authz: FakeAuthz, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    seen_depth: list[str] = []

    def fake_build(_lakehouse_id: uuid.UUID, *, settings: object, depth: str) -> LakehouseSchemaOut:
        seen_depth.append(depth)
        return LakehouseSchemaOut(
            namespaces=[NamespaceSchemaOut(name="sales", tables=[TableSchemaOut(name="orders")])]
        )

    monkeypatch.setattr("loom_query.routers.lakehouses.build_schema_tree", fake_build)

    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema",
            json={"principal": _principal_json(principal)},
        )

    assert response.status_code == 200
    assert seen_depth == ["tables"]
    body = response.json()
    assert body == {"namespaces": [{"name": "sales", "tables": [{"name": "orders"}]}]}


async def test_depth_columns_is_forwarded_and_included(
    fake_authz: FakeAuthz, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    seen_depth: list[str] = []

    def fake_build(_lakehouse_id: uuid.UUID, *, settings: object, depth: str) -> LakehouseSchemaOut:
        seen_depth.append(depth)
        return LakehouseSchemaOut(
            namespaces=[
                NamespaceSchemaOut(
                    name="sales",
                    tables=[
                        TableSchemaOut(name="orders", columns=[ColumnOut(name="id", type="int64")])
                    ],
                )
            ]
        )

    monkeypatch.setattr("loom_query.routers.lakehouses.build_schema_tree", fake_build)

    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=columns",
            json={"principal": _principal_json(principal)},
        )

    assert response.status_code == 200
    assert seen_depth == ["columns"]
    body = response.json()
    assert body["namespaces"][0]["tables"][0]["columns"] == [{"name": "id", "type": "int64"}]


async def test_invalid_depth_value_is_422(fake_authz: FakeAuthz, principal: Principal) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema?depth=bogus",
            json={"principal": _principal_json(principal)},
        )
    assert response.status_code == 422


async def test_missing_shared_secret_is_401(fake_authz: FakeAuthz, principal: Principal) -> None:
    """`lakehouses.router` đứng sau CÙNG `dependencies=` mà `query.router`
    dùng — không phải một phép kiểm riêng dán vào handler này."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    app = create_app(authz=fake_authz)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema",
            json={"principal": _principal_json(principal)},
        )
    assert response.status_code == 401


async def test_correct_secret_and_role_reaches_the_handler(
    fake_authz: FakeAuthz, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chốt chống-xanh-rỗng: nếu MỌI request đều 401/403 bất kể trạng thái,
    các phép kiểm phía trên xanh vì lý do sai. `http_client` (đã có header
    đúng) + `grant("viewer")` phải cho 200 thật, không phải một mã lỗi nào."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    monkeypatch.setattr(
        "loom_query.routers.lakehouses.build_schema_tree",
        lambda *_a, **_kw: LakehouseSchemaOut(namespaces=[]),
    )
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.request(
            "GET",
            f"/api/v1/lakehouses/{lakehouse_id}/schema",
            json={"principal": _principal_json(principal)},
        )
    assert response.status_code == 200
    assert response.json() == {"namespaces": []}
