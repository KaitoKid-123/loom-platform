"""Test HTTP đầy đủ của `loom-query`, KHÔNG cần Docker.

`authz` luôn được tiêm giả lập (`FakeAuthz`, xem `tests/conftest.py`) — điều
các phép kiểm dưới đây khẳng định là bộ ba route + cổng quyền + task nền nối
đúng với nhau, không phải luật RBAC (đã có differential test riêng bên
`loom-api`, xem docstring `authz.py`).

`test_forbidden_query_never_touches_the_catalog` là chứng minh đỏ 2 của Task
6 (chuyển bước "thiếu viewer -> 403" xuống SAU bước "mở bảng"): `catalog_uri`/
`s3_endpoint` trỏ vào cổng 1 trên loopback — không server nào lắng nghe ở đó
nên bất kỳ kết nối nào cũng bị từ chối gần như tức thì. Miễn là cổng quyền
CHẶN trước khi chạm mạng, response vẫn là 403 sạch dù cấu hình catalog hỏng
hoàn toàn. Nếu cổng quyền lỡ chạy SAU khi mở bảng, request sẽ ăn lỗi kết nối
(hoặc bất kỳ ngoại lệ nào httpx/pyiceberg ném ra khi nói chuyện với cổng 1)
thay vì 403 — và phép kiểm dưới đây đỏ theo đúng cách đó, không cần một spy
nào theo dõi lệnh gọi `build_catalog`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from loom_core.schemas import Principal
from loom_query.config import Settings
from loom_query.main import create_app
from loom_query.schemas import ColumnOut
from loom_query.store import QueryStore

from .conftest import FakeAuthz, http_client

# `fake_authz`/`principal` là fixture của `tests/conftest.py`, tiêm thẳng theo
# tên tham số — không cần import. `FakeAuthz` ở trên chỉ để chú thích kiểu.

# Cổng 1 trên loopback: không service nào lắng nghe ở đó (nó nằm trong dải
# cổng "well-known" mà không tiến trình thường nào bind), nên kết nối bị từ
# chối gần như ngay lập tức — không cần một timeout dài để test này nhanh.
UNREACHABLE = "http://127.0.0.1:1"


def _body(
    lakehouse_id: uuid.UUID,
    sql: str,
    principal: Principal,
    *,
    workspace_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "lakehouse_id": str(lakehouse_id),
        # Giá trị mặc định ngẫu nhiên: hầu hết test dưới đây dùng bảng HAI
        # phần, và `run_gate` không hỏi gì về workspace cho trường hợp đó — chỉ
        # test tên BA phần mới cần truyền `workspace_id=` tường minh.
        "workspace_id": str(workspace_id or uuid.uuid4()),
        "sql": sql,
        "principal": {
            "user_id": str(principal.user_id),
            "subject": principal.subject,
            "email": principal.email,
            "display_name": principal.display_name,
            "groups": list(principal.groups),
        },
    }


@pytest.fixture
def unreachable_settings() -> Settings:
    return Settings(catalog_uri=f"{UNREACHABLE}/catalog", s3_endpoint=UNREACHABLE)


async def test_bad_sql_is_rejected_with_400_and_line_column(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.post(
            "/api/v1/query",
            json=_body(uuid.uuid4(), "SELECT 1\nFROM foo\nWHERE ((( )", principal),
        )

    assert response.status_code == 400
    errors = response.json()["detail"]["errors"]
    assert errors[0]["line"] == 3


async def test_unqualified_table_name_is_rejected_with_400(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.post(
            "/api/v1/query", json=_body(uuid.uuid4(), "SELECT * FROM orders", principal)
        )

    assert response.status_code == 400
    assert "unqualified" in response.json()["detail"] or "namespace" in response.json()["detail"]


async def test_three_part_table_name_with_an_unresolvable_lakehouse_is_forbidden(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Tên bảng BA phần giờ được HỖ TRỢ (không còn 400 "chưa hỗ trợ" của Task
    6) — nhưng "lh" không được đăng ký qua `fake_authz.register_lakehouse`
    trong `workspace_id` của request, nên nó không phân giải được. Kết quả
    vẫn phải là 403 (không phân biệt được với "có nhưng không có quyền"),
    KHÔNG phải 404 — xem `tests/test_query_authz_gate.py` cho phép kiểm chi
    tiết hơn về tính indistinguishable đó."""
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.post(
            "/api/v1/query", json=_body(uuid.uuid4(), "SELECT * FROM lh.ns.t", principal)
        )

    assert response.status_code == 403


async def test_forbidden_query_never_touches_the_catalog(
    unreachable_settings: Settings, fake_authz: FakeAuthz, principal: Principal
) -> None:
    # `fake_authz` không được `grant()` gì — mọi bảng bị từ chối.
    app = create_app(settings=unreachable_settings, authz=fake_authz)
    async with http_client(app) as client:
        response = await client.post(
            "/api/v1/query", json=_body(uuid.uuid4(), "SELECT * FROM ns.orders", principal)
        )

    assert response.status_code == 403


async def test_allowed_query_reaches_the_background_runner(
    fake_authz: FakeAuthz, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Không cần Iceberg thật ở đây — xem `tests/integration/` cho SELECT
    thật. Thay `runner.execute` bằng một bản giả để kiểm ĐÚNG một việc: `POST`
    lập lịch task nền, và `GET` đọc lại kết quả nó ghi vào `store`."""

    async def fake_execute(**kwargs: Any) -> None:
        store: QueryStore = kwargs["store"]
        await store.set_succeeded(
            kwargs["query_id"], [ColumnOut(name="a", type="int64")], [[1], [2]]
        )

    monkeypatch.setattr("loom_query.routers.query.runner.execute", fake_execute)

    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        create_response = await client.post(
            "/api/v1/query", json=_body(lakehouse_id, "SELECT * FROM ns.orders", principal)
        )
        assert create_response.status_code == 202
        query_id = create_response.json()["query_id"]

        body: dict[str, Any] = {}
        for _ in range(100):
            status_response = await client.get(f"/api/v1/query/{query_id}")
            body = status_response.json()
            if body["status"] != "running":
                break
            await asyncio.sleep(0.01)

    assert body["status"] == "succeeded"
    assert body["rows"] == [[1], [2]]
    assert body["columns"] == [{"name": "a", "type": "int64"}]


async def test_get_unknown_query_id_is_404(fake_authz: FakeAuthz) -> None:
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.get(f"/api/v1/query/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_delete_marks_a_running_query_cancelled(
    fake_authz: FakeAuthz, principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    never_finishes = asyncio.Event()

    async def fake_execute(**_: object) -> None:
        await never_finishes.wait()

    monkeypatch.setattr("loom_query.routers.query.runner.execute", fake_execute)

    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    app = create_app(authz=fake_authz)
    try:
        async with http_client(app) as client:
            create_response = await client.post(
                "/api/v1/query", json=_body(lakehouse_id, "SELECT * FROM ns.orders", principal)
            )
            query_id = create_response.json()["query_id"]

            delete_response = await client.delete(f"/api/v1/query/{query_id}")
            assert delete_response.status_code == 202
            assert delete_response.json() == {"status": "cancelled"}

            status_response = await client.get(f"/api/v1/query/{query_id}")
            assert status_response.json()["status"] == "cancelled"
    finally:
        # Nhả task nền đang treo TRƯỚC khi event loop của test đóng — không có
        # dòng này, `asyncio.Event` không bao giờ `set()` để lại một task
        # pending mãi mãi và pytest-asyncio cảnh báo "Task was destroyed but
        # it is pending" ở lần chạy sau.
        never_finishes.set()


async def test_delete_unknown_query_id_is_404(fake_authz: FakeAuthz) -> None:
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.delete(f"/api/v1/query/{uuid.uuid4()}")
    assert response.status_code == 404
