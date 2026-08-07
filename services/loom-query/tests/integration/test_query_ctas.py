"""CTAS trên lakehouse THẬT — sáu chứng minh đỏ bắt buộc của Giai đoạn 2c.

Trước task này, `CREATE TABLE ... AS SELECT` LUÔN hỏng: `loom_sql.deps.
dependencies()` coi đích CTAS là một bảng cần ĐỌC, y hệt một bảng nguồn, nên
`authz.run_gate` đòi quyền trên một bảng chưa tồn tại và `runner._run_sync`
cố `Lakehouse.scan()` nó TRƯỚC KHI câu `CREATE` kịp chạy — "table not found",
không bao giờ tới lượt CREATE. Hậu quả thứ hai, nặng hơn: GHI chỉ đòi
`viewer`, y như ĐỌC, dù `loom_core.roles.ACTION_MATRIX` xếp `item.update` vào
`contributor` — một lỗ RBAC.

File này khoá cả sáu:

  1. `test_ctas_creates_a_real_iceberg_table_readable_afterwards` — vế
     KHẲNG ĐỊNH: CTAS chạy được, bảng xuất hiện trong `list_tables()` VÀ đọc
     lại đúng dòng.
  2. `test_ctas_requires_contributor_viewer_is_forbidden_over_http` — viewer
     chạy CTAS -> 403.
  3. (đọc vẫn chỉ đòi viewer) — đã canh ở `test_query_select.py`; không lặp
     lại ở đây để tránh hai file cùng đứng/đổ vì một lý do.
  4. `test_ctas_destination_that_does_not_exist_yet_is_not_scanned` — đích
     CHƯA TỒN TẠI không được `Lakehouse.scan()`.
  5. `test_ctas_destination_bytes_are_not_counted_toward_the_scan_cap` — đích
     không tính vào trần byte quét.
  6. `test_self_referencing_insert_requires_contributor_and_still_scans_the_table`
     — `INSERT INTO t SELECT * FROM t` đòi contributor VÀ vẫn quét `t`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from loom_core.schemas import Principal
from loom_iceberg import Lakehouse, build_catalog
from loom_query.config import Settings
from loom_query.main import create_app

from ..conftest import FakeAuthz, http_client

# `fake_authz`/`principal` (tests/conftest.py) và `app_settings`/`seeded_table`/
# `lakehouse_id`/`lakekeeper`/`s3_endpoint`/`warehouse_name` (tests/integration/
# conftest.py) là fixture, tiêm thẳng theo tên tham số — không cần import.

pytestmark = pytest.mark.integration


def _body(lakehouse_id: uuid.UUID, sql: str, principal: Principal) -> dict[str, Any]:
    return {
        "lakehouse_id": str(lakehouse_id),
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
    app: Any, lakehouse_id: uuid.UUID, sql: str, principal: Principal
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


async def test_ctas_creates_a_real_iceberg_table_readable_afterwards(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """Vế KHẲNG ĐỊNH bắt buộc — không có nó, mọi phép từ chối bên dưới xanh vô
    nghĩa. `contributor` (không phải `viewer`) vì GHI đòi `item.update`."""
    assert seeded_table == "sales.orders"
    fake_authz.grant(lakehouse_id, "contributor")
    app = create_app(settings=app_settings, authz=fake_authz)

    body = await _run_and_wait(
        app, lakehouse_id, "CREATE TABLE sales.orders_ctas AS SELECT * FROM sales.orders", principal
    )
    assert body["status"] == "succeeded", body

    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    lakehouse = Lakehouse(catalog)
    assert "sales.orders_ctas" in [t.qualified for t in lakehouse.list_tables("sales")]

    result = lakehouse.scan("sales.orders_ctas").read_all()
    assert result.sort_by("id").to_pydict() == {
        "id": [1, 2, 3],
        "amount": [10.0, 20.0, 30.0],
    }


async def test_ctas_requires_contributor_viewer_is_forbidden_over_http(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """Chứng minh đỏ 2 (bắt buộc): `viewer` chạy CTAS -> 403 NGAY trong response
    của `POST` (cổng quyền chạy đồng bộ, chưa từng chạm Lakekeeper)."""
    fake_authz.grant(lakehouse_id, "viewer")
    app = create_app(settings=app_settings, authz=fake_authz)

    async with http_client(app) as client:
        response = await client.post(
            "/api/v1/query",
            json=_body(
                lakehouse_id,
                "CREATE TABLE sales.orders_viewer_attempt AS SELECT * FROM sales.orders",
                principal,
            ),
        )

    assert response.status_code == 403


async def test_ctas_destination_that_does_not_exist_yet_is_not_scanned(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chứng minh đỏ 4 (bắt buộc): đích CTAS (`orders_no_scan`, CHƯA tồn tại)
    không được `Lakehouse.scan()` — chỉ nguồn (`sales.orders`) mới được quét.

    Cho runner quét CẢ đích (đổi cài đặt để đưa MỌI `resolved_tables`, không
    chỉ vế đọc, vào vòng lặp đăng ký view) làm bài này ĐỎ theo đúng cách CTAS
    từng hỏng trước task này: `Lakehouse.scan("sales.orders_no_scan")` ném lỗi
    "table not found" của PyIceberg, và `status` không còn là `succeeded`.
    """
    scanned: list[str] = []
    original_scan = Lakehouse.scan

    def spying_scan(self: Lakehouse, qualified: str) -> Any:
        scanned.append(qualified)
        return original_scan(self, qualified)

    monkeypatch.setattr(Lakehouse, "scan", spying_scan)

    fake_authz.grant(lakehouse_id, "contributor")
    app = create_app(settings=app_settings, authz=fake_authz)

    body = await _run_and_wait(
        app,
        lakehouse_id,
        "CREATE TABLE sales.orders_no_scan AS SELECT * FROM sales.orders",
        principal,
    )

    assert body["status"] == "succeeded", body
    assert scanned == ["sales.orders"], (
        f"đích CTAS bị quét: {scanned} — một đích CHƯA TỒN TẠI không có gì để quét"
    )


async def test_ctas_destination_bytes_are_not_counted_toward_the_scan_cap(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    fake_authz: FakeAuthz,
    principal: Principal,
) -> None:
    """Chứng minh đỏ 5 (bắt buộc): trần byte đặt ĐÚNG BẰNG kích thước THẬT của
    nguồn (`sales.orders`) — CTAS vẫn phải THÀNH CÔNG. Nếu runner tính cả đích
    (chưa tồn tại) vào tổng byte quét, `check_scan_bytes` sẽ gọi
    `Lakehouse.scan_size_bytes()` trên một bảng không tồn tại và ném lỗi "table
    not found" — một cách hỏng KHÁC với "vượt trần byte" (đã tự tay kiểm bằng
    cách tạm đưa `resolved_tables` (thay vì chỉ vế đọc) vào `scan_targets`: ra
    đúng lỗi "table not found", không phải "byte cap")."""
    fake_authz.grant(lakehouse_id, "contributor")
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    real_size = Lakehouse(catalog).scan_size_bytes(seeded_table)
    exact_cap = app_settings.model_copy(update={"max_scan_bytes": real_size})
    app = create_app(settings=exact_cap, authz=fake_authz)

    body = await _run_and_wait(
        app,
        lakehouse_id,
        "CREATE TABLE sales.orders_tiny_cap AS SELECT * FROM sales.orders",
        principal,
    )

    assert body["status"] == "succeeded", body


async def test_self_referencing_insert_requires_contributor_and_still_scans_the_table(
    app_settings: Settings,
    seeded_table: str,
    lakehouse_id: uuid.UUID,
    fake_authz: FakeAuthz,
    principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chứng minh đỏ 6 (bắt buộc): `INSERT INTO t SELECT * FROM t` phải đòi
    `contributor` (viewer -> 403 ngay, chưa từng chạm Lakekeeper) VÀ, khi có
    contributor, `t` vẫn nằm trong tập ĐỌC — `Lakehouse.scan("sales.orders")`
    THẬT SỰ được gọi.

    KHÔNG khẳng định `status == "succeeded"` ở nhánh contributor: `runner`
    CHƯA có đường commit thật cho `INSERT INTO ... SELECT` (xem docstring
    `loom_sql.deps.write_target` — CTAS là đường ghi DUY NHẤT Giai đoạn 2c xây;
    `INSERT` được RBAC đúng và vế đọc được quét đúng, nhưng chạy nó cho một
    `failed` rõ ràng thay vì âm thầm 'thành công' mà không ghi gì)."""
    scanned: list[str] = []
    original_scan = Lakehouse.scan

    def spying_scan(self: Lakehouse, qualified: str) -> Any:
        scanned.append(qualified)
        return original_scan(self, qualified)

    monkeypatch.setattr(Lakehouse, "scan", spying_scan)

    fake_authz.grant(lakehouse_id, "viewer")
    app = create_app(settings=app_settings, authz=fake_authz)
    async with http_client(app) as client:
        response = await client.post(
            "/api/v1/query",
            json=_body(
                lakehouse_id, "INSERT INTO sales.orders SELECT * FROM sales.orders", principal
            ),
        )
    assert response.status_code == 403
    assert scanned == [], "cổng quyền phải chặn TRƯỚC khi runner chạm Lakekeeper"

    fake_authz.grant(lakehouse_id, "contributor")
    await _run_and_wait(
        app, lakehouse_id, "INSERT INTO sales.orders SELECT * FROM sales.orders", principal
    )
    assert scanned == ["sales.orders"], "t vừa đọc vừa ghi vẫn phải được quét"
