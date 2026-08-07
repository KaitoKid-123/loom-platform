"""Chứng minh đỏ 5 (và một phần chứng minh 1) với Lakekeeper + MinIO THẬT —
không con giả.

Bốn chứng minh khác (`test_root_credential_guard.py`,
`test_warehouse_provisioning_lifecycle.py`,
`test_lakehouse_creation_provisions_warehouse.py`) cố ý KHÔNG cần Docker
ngoài Postgres — chúng kiểm HÀNH VI phối hợp giữa `ItemStore`/
`warehouse_provisioning`/router, không kiểm Lakekeeper thật hoạt động đúng.
Bài này thì cần: "xoá mềm không xoá warehouse, và warehouse còn dùng được"
chỉ có ý nghĩa nếu ta hỏi chính Lakekeeper thật, không hỏi một con giả đã được
lập trình sẵn để trả lời đúng.

Không có thao tác "bỏ-xoá" (undelete) trong Giai đoạn 1b — xem
`test_restoring_a_deleted_item_is_a_404` ở `test_item_store.py`, một chứng
minh còn sống rằng phục hồi NỘI DUNG (`restore_version`) chỉ chạy trên item
đang sống, không un-delete nó. Vì vậy "restore dùng lại được" ở đây được kiểm
ở đúng tầng nó có ý nghĩa THẬT hôm nay: tầng lưu trữ. Warehouse — và dữ liệu
Iceberg bên trong nó — phải còn nguyên vẹn và truy vấn được sau một lần xoá
mềm, để bất kỳ tính năng "bỏ-xoá" nào thêm sau này chỉ cần lật `item.state`
lại, không cần cấp phát lại gì cả.
"""

import uuid

import httpx
import pyarrow as pa
import pytest
from sqlalchemy import update

from loom_api.models import ACTIVE, Item
from loom_core.roles import Role
from loom_iceberg import Lakehouse, build_catalog
from loom_storage.credentials import prefix_for_lakehouse

pytestmark = pytest.mark.integration

_LAKEKEEPER_TIMEOUT = 10.0


@pytest.fixture
async def contributor(api_world_with_lakekeeper):
    await api_world_with_lakekeeper.grant(
        ("workspace", api_world_with_lakekeeper.ws_a), Role.contributor
    )
    return api_world_with_lakekeeper


def _warehouse_by_name(lakekeeper: str, name: str) -> dict[str, object] | None:
    """Tra theo `name`, KHÔNG theo `id`.

    Lakekeeper trả về `id` là UUID NỘI BỘ của chính nó (sinh ngẫu nhiên lúc
    tạo) — HOÀN TOÀN khác `name`, thứ mà `create_warehouse()` đặt bằng
    `str(item_id)`. `RestCatalog`/`build_catalog(warehouse=...)` (và
    `GET /catalog/v1/config?warehouse=...` bên dưới nó) phân giải theo `name`
    này, không theo `id` nội bộ — khớp đúng quy ước `warehouse=str(lakehouse_
    id)` mà `loom_query.runner` giả định. Tra theo `id` sẽ luôn ra `None` dù
    warehouse tồn tại và hoạt động bình thường.
    """
    response = httpx.get(f"{lakekeeper}/management/v1/warehouse", timeout=_LAKEKEEPER_TIMEOUT)
    response.raise_for_status()
    for warehouse in response.json()["warehouses"]:
        if warehouse["name"] == name:
            return warehouse  # type: ignore[no-any-return]
    return None


async def test_creating_a_lakehouse_provisions_a_real_warehouse(
    contributor, lakekeeper, s3_endpoint
):
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "lakehouse",
            "name": "kho-that",
            "display_name": "Kho thật",
            "definition": {"schema_version": 1},
        },
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]

    warehouse = _warehouse_by_name(lakekeeper, item_id)
    assert warehouse is not None, "khong thay warehouse trong Lakekeeper that"
    assert warehouse["name"] == item_id
    assert warehouse["status"] == "active"
    # Lakekeeper chuẩn hoá bỏ dấu `/` cuối khi lưu — so sánh sau khi rstrip.
    expected_prefix = prefix_for_lakehouse(contributor.ws_a, uuid.UUID(item_id)).rstrip("/")
    assert warehouse["storage-profile"]["key-prefix"] == expected_prefix


async def test_soft_delete_leaves_the_warehouse_intact_and_usable(
    contributor, lakekeeper, s3_endpoint
):
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "lakehouse",
            "name": "kho-se-bi-xoa-mem",
            "display_name": "Kho sẽ bị xoá mềm",
            "definition": {"schema_version": 1},
        },
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]

    # Đặt dữ liệu THẬT vào warehouse trước khi xoá — đúng tình huống mà spec
    # nhắc tới (Lakekeeper từ chối xoá warehouse còn bảng, `409
    # WarehouseNotEmpty`), và là điều kiện để "còn dùng được" bên dưới có gì
    # để chứng minh chứ không phải kiểm một warehouse rỗng.
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=item_id, s3_endpoint=s3_endpoint
    )
    lakehouse = Lakehouse(catalog)
    lakehouse.create_namespace("sales")
    lakehouse.create_from(
        "sales.orders",
        pa.table({"id": pa.array([1, 2, 3], type=pa.int64())}),
    )

    delete_response = await contributor.client.delete(f"/api/v1/items/{item_id}")
    assert delete_response.status_code == 204

    # 1. Warehouse CÒN ĐÓ trong Lakekeeper thật — không bị xoá theo item.
    warehouse = _warehouse_by_name(lakekeeper, item_id)
    assert warehouse is not None, "warehouse bi xoa theo item — xoa mem PHAI khong cham Lakekeeper"
    assert warehouse["status"] == "active"

    # 2. Warehouse CÒN DÙNG ĐƯỢC: mở lại catalog, đọc lại đúng dữ liệu đã ghi.
    #    Đây là ý nghĩa thật của "restore dùng lại được" ở tầng lưu trữ — Giai
    #    đoạn 1b chưa có thao tác bỏ-xoá (xem docstring module), nên phần còn
    #    lại (`item.state` quay về ACTIVE) là một phép ghi DB đơn thuần, kiểm
    #    ở bước 3.
    reopened = Lakehouse(
        build_catalog(
            catalog_uri=f"{lakekeeper}/catalog", warehouse=item_id, s3_endpoint=s3_endpoint
        )
    )
    assert "sales" in reopened.list_namespaces()
    table = reopened.scan("sales.orders").read_all()
    assert sorted(table.column("id").to_pylist()) == [1, 2, 3]

    # 3. Mô phỏng một thao tác "bỏ-xoá" tương lai: lật state về ACTIVE bằng một
    #    UPDATE trực tiếp (Giai đoạn 1b chưa có endpoint cho việc này — xem
    #    `test_restoring_a_deleted_item_is_a_404`). Nếu xoá mềm đã lỡ xoá luôn
    #    warehouse thì bước này vẫn "thành công" ở Postgres nhưng item vẫn vô
    #    dụng — bước 1/2 ở trên mới là phép kiểm thật, bước này chỉ xác nhận
    #    phía control-plane cũng sẵn sàng ngay khi có nó.
    async with contributor.engine.connect() as connection:
        await connection.execute(
            update(Item).where(Item.id == uuid.UUID(item_id)).values(state=ACTIVE)
        )
        await connection.commit()
    get_response = await contributor.client.get(f"/api/v1/items/{item_id}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == item_id


async def test_soft_deleting_a_brand_new_empty_lakehouse_still_keeps_the_warehouse(
    contributor, lakekeeper, s3_endpoint
):
    """Cùng khẳng định với phép kiểm trên, nhưng warehouse RỖNG — chưa có
    namespace/bảng nào.

    Lakekeeper tự chặn xoá một warehouse CÒN bảng (`409 WarehouseNotEmpty`,
    xem phép kiểm trên), nên một mutation vô tình thêm lệnh xoá vào
    `soft_delete` sẽ bị chính Lakekeeper cản lại NẾU warehouse không rỗng —
    và phép kiểm trên sẽ không phát hiện ra mutation đó, dù nó thật sự tồn
    tại. Một lakehouse mới tạo rồi bị xoá ngay (trước khi ai kịp thêm bảng
    nào) là một tình huống THẬT — và ở đó không có tấm lưới an toàn nào của
    Lakekeeper đứng chắn: nếu `soft_delete` lỡ gọi xoá warehouse, warehouse
    RỖNG sẽ biến mất thật sự. Đây là phép kiểm PHÁT HIỆN được lớp lỗi đó.
    """
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "lakehouse",
            "name": "kho-rong-xoa-ngay",
            "display_name": "Kho rỗng xoá ngay",
            "definition": {"schema_version": 1},
        },
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]

    delete_response = await contributor.client.delete(f"/api/v1/items/{item_id}")
    assert delete_response.status_code == 204

    warehouse = _warehouse_by_name(lakekeeper, item_id)
    assert warehouse is not None, (
        "warehouse RỖNG bị xoá theo item — không có Lakekeeper 409 nào cản lại "
        "được một warehouse trống, nên đây là lớp lỗi mà bản thân Lakekeeper "
        "không tự bảo vệ ta khỏi nó"
    )
    assert warehouse["status"] == "active"
