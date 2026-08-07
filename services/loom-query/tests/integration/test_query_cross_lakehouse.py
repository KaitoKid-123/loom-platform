"""`JOIN` THẬT giữa hai lakehouse — mỗi bên đọc ĐÚNG dữ liệu của mình.

Đây là chứng minh đỏ 2 của bản sửa "một catalog cho mỗi lakehouse" (xem module
docstring `runner.py`): hai warehouse Iceberg RIÊNG, mỗi cái có MỘT bảng cùng
tên `finance.reports` nhưng dữ liệu KHÁC NHAU — đúng kịch bản mà một catalog
DuckDB dùng chung cho cả hai lakehouse sẽ làm view của bên chèn sau ĐÈ lên bên
chèn trước, và query vẫn CHẠY ĐƯỢC nhưng trả dữ liệu SAI, không ném lỗi nào.

**Chứng minh đỏ bắt buộc:** trong `runner._run_sync`, đổi tạm cả hai lời gọi
`lakehouse_for(table.lakehouse_id)` thành `lakehouse_for(resolved_tables[0].
lakehouse_id)` — mô phỏng lại đúng lỗi "một catalog cho mọi bảng" (bản trước
bản sửa "một catalog mỗi lakehouse"). `dependencies()` sắp `TableRef` theo
`(namespace, name)`, và `"b.finance"` < `"finance"` (so chuỗi, `b` < `f`), nên
`resolved_tables[0]` là bảng BA phần trỏ tới lakehouse B — nghĩa là CẢ HAI bên
của `JOIN` bị ép đọc từ catalog của B. Phép kiểm dưới đây phải ĐỎ ở đúng khẳng
định `body["rows"] == [...]`, KHÔNG phải vì `status != "succeeded"` — nếu đỏ ở
`status`, đó là dấu hiệu sabotage tạo ra một lỗi ném ra thay vì dữ liệu sai, và
không chứng minh đúng thứ cần chứng minh.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pyarrow as pa
import pytest
from httpx import ASGITransport, AsyncClient

from loom_core.schemas import Principal
from loom_iceberg import Lakehouse, build_catalog, create_warehouse
from loom_query.config import Settings
from loom_query.main import create_app

from ..conftest import FakeAuthz, a_principal
from .conftest import ROOT_PASSWORD, ROOT_USER

pytestmark = pytest.mark.integration


def _body(
    lakehouse_id: uuid.UUID, workspace_id: uuid.UUID, sql: str, principal: Principal
) -> dict[str, Any]:
    return {
        "lakehouse_id": str(lakehouse_id),
        "workspace_id": str(workspace_id),
        "sql": sql,
        "principal": {
            "user_id": str(principal.user_id),
            "subject": principal.subject,
            "email": principal.email,
            "display_name": principal.display_name,
            "groups": list(principal.groups),
        },
    }


def _seed_finance_reports(
    *, lakekeeper: str, s3_endpoint: str, bucket: str, lakehouse_id: uuid.UUID, labels: list[str]
) -> None:
    """Dựng một warehouse RIÊNG cho `lakehouse_id`, với bảng `finance.reports`
    hai dòng — `id` giống hệt giữa hai lakehouse (để `JOIN ... ON id` khớp
    được), `label` KHÁC NHAU (để phân biệt được bên nào trả dữ liệu của ai)."""
    warehouse_name = str(lakehouse_id)
    create_warehouse(
        lakekeeper,
        name=warehouse_name,
        bucket=bucket,
        key_prefix=f"loom-query-test/{lakehouse_id}",
        s3_endpoint=s3_endpoint,
        access_key=ROOT_USER,
        secret_key=ROOT_PASSWORD,
    )
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    lakehouse = Lakehouse(catalog)
    lakehouse.create_namespace("finance")
    lakehouse.create_from(
        "finance.reports",
        pa.table(
            {
                "id": pa.array([1, 2], type=pa.int64()),
                "label": pa.array(labels, type=pa.string()),
            }
        ),
    )


async def test_join_across_two_lakehouses_returns_the_correct_side_for_each(
    lakekeeper: str,
    s3_endpoint: str,
    bucket: str,
    fake_authz: FakeAuthz,
) -> None:
    """Hai lakehouse THẬT, cả hai có `finance.reports` — A qua tên hai phần
    (lakehouse của chính request), B qua tên ba phần (`b.finance.reports`).
    `JOIN` trên `id` phải trả `label` ĐÚNG của từng bên — không lệch, không
    trộn."""
    principal = a_principal()
    workspace_id = uuid.uuid4()
    lakehouse_a = uuid.uuid4()
    lakehouse_b = uuid.uuid4()

    _seed_finance_reports(
        lakekeeper=lakekeeper,
        s3_endpoint=s3_endpoint,
        bucket=bucket,
        lakehouse_id=lakehouse_a,
        labels=["A1", "A2"],
    )
    _seed_finance_reports(
        lakekeeper=lakekeeper,
        s3_endpoint=s3_endpoint,
        bucket=bucket,
        lakehouse_id=lakehouse_b,
        labels=["B1", "B2"],
    )

    fake_authz.register_lakehouse(workspace_id, "b", lakehouse_b)
    fake_authz.grant(lakehouse_a, "viewer")
    fake_authz.grant(lakehouse_b, "viewer")

    settings = Settings(catalog_uri=f"{lakekeeper}/catalog", s3_endpoint=s3_endpoint)
    app = create_app(settings=settings, authz=fake_authz)
    transport = ASGITransport(app=app)
    sql = (
        "SELECT a.id AS id, a.label AS a_label, r.label AS b_label "
        "FROM finance.reports a JOIN b.finance.reports r ON a.id = r.id "
        "ORDER BY a.id"
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/query", json=_body(lakehouse_a, workspace_id, sql, principal)
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

    assert body["status"] == "succeeded", body
    assert body["rows"] == [[1, "A1", "B1"], [2, "A2", "B2"]], (
        "mỗi bên của JOIN phải trả đúng dữ liệu lakehouse của mình — thấy "
        f"{body['rows']}, một catalog DuckDB dùng chung cho cả hai lakehouse "
        "sẽ làm dữ liệu của bên này lộ ra ở bên kia mà không ném lỗi nào"
    )
