"""`GET /api/v1/lakehouses/{lakehouse_id}/schema` — cây `namespace -> bảng ->
cột` mà Lakehouse Explorer VÀ autocomplete SQL đều cần (Task 2, Giai đoạn 2c).

Cổng quyền ở đây NHẸ HƠN `authz.run_gate` của `/query` — không có SQL nào để
`validate`/`table_deps`, không có tên bảng ba phần nào cần `LakehouseResolver`
phân giải: `lakehouse_id` đã có sẵn NGUYÊN VẸN trên path, nên chỉ còn ĐÚNG một
câu hỏi để hỏi `AuthzPort.roles_for_items` — "principal này có viewer trên
CHÍNH id này không". Vẫn dùng lại `AuthzPort` (Protocol dùng chung với
`run_gate`) thay vì tự bịa một cách hỏi khác — lý do y hệt đã ghi ở module
docstring `authz.py`: `loom-query` KHÔNG tự tính quyền, chỉ hỏi.

**Lakehouse không tồn tại và principal thiếu quyền phải KHÔNG PHÂN BIỆT ĐƯỢC
— cả hai đều 403 `SchemaForbidden`, cùng thông điệp.** `loom-query` không có
cách nào tự biết "id này có tồn tại hay không" ngoài việc hỏi
`/internal/authz/items` — và `AuthzItemsResponse.roles` (xem docstring
`loom_core.schemas`) đã cố ý gộp hai trường hợp đó thành cùng một `null`
(differential test canh bất biến này nằm ở `services/api/tests/integration/
test_internal_authz_api.py::test_missing_item_and_forbidden_item_are_
indistinguishable`). `run_schema_gate` bên dưới chỉ có ĐÚNG MỘT nhánh cho
`role is None` — không có nhánh "id không tồn tại" nào khác để phân biệt,
đúng cấu trúc mà `authz.QueryForbidden` đã dùng cho `/query`. **Chỗ có thể vô
tình phá bất biến này không phải ở đây — là ở `loom-api`'s route proxy: nếu nó
thêm một bước tra cứu database kiểu `_lakehouse_workspace_id` (như route
`POST /query` đang làm) TRƯỚC khi chuyển tiếp, một `lakehouse_id` không tồn
tại sẽ nhận 404 từ chính `loom-api`, không bao giờ tới lượt `loom-query` trả
lời** — xem `services/api/tests/integration/test_lakehouse_schema_proxy.py`
cho phép kiểm canh đúng lỗ đó.

**`depth=tables` (mặc định) so với `depth=columns`: quyết định có ĐO, không
đoán** (spec Giai đoạn 2c bắt buộc "đo trước khi chốt" sau bốn lần đoán sai ở
Giai đoạn 2a). Đo trên container thật (MinIO + Postgres + Lakekeeper, xem
`tests/integration/test_lakehouse_schema_size_benchmark.py`), một lakehouse
**200 bảng thật x 30 cột thật**:

  - `list_namespaces` + `list_tables`: ~13ms tổng, không phụ thuộc số cột.
  - `schema()` cho CẢ 200 bảng (mỗi bảng một round trip `load_table` riêng
    tới Lakekeeper — KHÔNG có API "lấy schema hàng loạt"): ~1.44s tổng
    (~7.2ms/bảng), và con số này CO GIÃN TUYẾN TÍNH theo số bảng, không phải
    hằng số.
  - Kích thước JSON: ~221 KB (đầy đủ cột) so với ~4 KB (chỉ tên bảng).

  Kích thước (~221 KB) nằm gọn trong "vài trăm KB", nhưng độ trễ (~1.45s,
  CATALOG LẠNH — đúng những gì một request thật gặp, không phải catalog đã
  "ấm") VƯỢT ngưỡng "dưới ~1 giây" mà spec đặt ra cho phương án "trả cả cây
  một lần" — và với một lakehouse có NHIỀU HƠN 200 bảng (hoàn toàn có thật),
  con số này chỉ tăng thêm, không giảm. Quyết định: **tách cột ra sau
  `?depth=`, mặc định `tables`** — Explorer (Task 3) mở lakehouse ra chỉ cần
  tên namespace/bảng (~13ms, ~4KB, không phụ thuộc số cột), và trả tiền ~7ms
  MỘT bảng (không phải 200) khi người dùng thật sự mở nó ra xem cột.
  `depth=columns` (cây ĐẦY ĐỦ, đúng hình dạng đã ghi ở docstring
  `LakehouseSchemaOut`) vẫn phục vụ đúng nhu cầu autocomplete khi cần toàn bộ
  cột của một lakehouse trong một lần gọi.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import HTTPException, status

from loom_core.schemas import Principal
from loom_iceberg import Lakehouse, build_catalog
from loom_query.authz import AuthzPort
from loom_query.config import Settings
from loom_query.schemas import ColumnOut, LakehouseSchemaOut, NamespaceSchemaOut, TableSchemaOut

Depth = Literal["tables", "columns"]


class SchemaForbidden(HTTPException):
    """403 — thiếu viewer trên `lakehouse_id`, HOẶC `lakehouse_id` không tồn
    tại chút nào. Cố ý KHÔNG nói lý do nào trong hai — xem module docstring
    cho lý do phân biệt hai trường hợp đó là rò rỉ sự tồn tại, đúng lỗ mà quy
    tắc 404-trước-403 của Giai đoạn 1 sinh ra để chặn."""

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_403_FORBIDDEN,
            detail="you do not have permission to read this lakehouse's schema",
        )


async def run_schema_gate(
    *, lakehouse_id: uuid.UUID, principal: Principal, authz: AuthzPort
) -> None:
    """Cổng quyền — MỘT id, không có gì để `validate`/phân giải. Ném
    `SchemaForbidden` nếu thiếu viewer (hoặc id không tồn tại — không phân
    biệt được, xem module docstring)."""
    roles = await authz.roles_for_items(principal, (lakehouse_id,))
    if roles.get(str(lakehouse_id)) is None:
        raise SchemaForbidden


def build_schema_tree(
    lakehouse_id: uuid.UUID, *, settings: Settings, depth: Depth
) -> LakehouseSchemaOut:
    """Phần CHẶN — mở một catalog Iceberg MỚI (đúng khuyến nghị của
    `loom_iceberg.catalog`, cùng cách `runner._run_sync` làm cho mỗi query) và
    liệt kê namespace/bảng/cột. GỌI QUA `asyncio.to_thread` ở người gọi
    (`routers/lakehouses.py`) — PyIceberg là thư viện đồng bộ, gọi thẳng trong
    một coroutine sẽ khoá event loop tới khi request tới Lakekeeper xong.

    `depth="tables"` bỏ qua HẲN vòng lặp `lakehouse.schema(...)` — không phải
    gọi rồi vứt kết quả, mà KHÔNG gọi chút nào — đây chính là điều làm
    `depth=tables` rẻ hơn `depth=columns` một cách CÓ Ý NGHĨA (xem module
    docstring: mỗi lời gọi `schema()` là một round trip `load_table` riêng).
    """
    catalog = build_catalog(
        catalog_uri=settings.catalog_uri,
        warehouse=str(lakehouse_id),
        s3_endpoint=settings.s3_endpoint,
    )
    lakehouse = Lakehouse(catalog)

    namespaces = []
    for namespace in lakehouse.list_namespaces():
        tables = []
        for table_info in lakehouse.list_tables(namespace):
            columns = None
            if depth == "columns":
                arrow_schema = lakehouse.schema(table_info.qualified)
                columns = [ColumnOut(name=f.name, type=str(f.type)) for f in arrow_schema]
            tables.append(TableSchemaOut(name=table_info.name, columns=columns))
        namespaces.append(NamespaceSchemaOut(name=namespace, tables=tables))
    return LakehouseSchemaOut(namespaces=namespaces)
