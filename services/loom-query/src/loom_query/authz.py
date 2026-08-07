"""Cổng quyền — phần quan trọng nhất của `loom-query`, chạy ĐỒNG BỘ trong POST.

Thứ tự dưới đây LÀ đặc tả, không phải chi tiết cài đặt (spec Giai đoạn 2b):

    1. sqlkit.validate(sql)      -> lỗi cú pháp -> 400 kèm dòng/cột
    2. sqlkit.table_deps(sql)    -> mọi bảng, kể cả trong CTE và subquery
    3. bảng -> item id           -> xem `_resolve_item_id`
    4. POST /internal/authz/items (qua `AuthzPort.roles_for_items`)
    5. thiếu viewer ở BẤT KỲ id nào -> 403, CHƯA từng chạm S3
    6. (ở module khác — `runner.py`) mới tới lượt build_catalog / mở bảng / chạy

`run_gate` dưới đây làm ĐÚNG năm bước đầu và dừng lại — nó không biết
`build_catalog` tồn tại, và đó là điểm mấu chốt: nếu bước 5 lỡ bị đảo xuống
sau bước 6 (một lỗi tái cấu trúc hoàn toàn có thể xảy ra), phép kiểm
`test_forbidden_query_never_touches_the_catalog` ở `tests/test_query_routes.py`
phải đỏ, vì lúc đó `run_gate` sẽ không còn là hàm DUY NHẤT chạy trước khi có
bất kỳ I/O nào ra ngoài tiến trình.

`loom-query` KHÔNG tự tính quyền. Giai đoạn 1 gom luật RBAC vào một nguồn
(`loom_api.permissions`) và giữ nó đúng bằng một differential test canh hai
đường đánh giá không trôi khỏi nhau; viết một luật thứ ba ở đây — dù chỉ là
"nếu vai trò không None thì cho qua" — là mở đúng con đường mà differential
test đó tồn tại để chặn. `AuthzPort` vì vậy chỉ có một việc: hỏi, không tính.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import HTTPException, status

from loom_core.schemas import Principal
from loom_sql import SqlError, TableRef, validate
from loom_sql.deps import dependencies

# DuckDB là engine DUY NHẤT ở Giai đoạn 2b — spec mục 5.9 để ngỏ việc đổi sang
# Trino cho Giai đoạn 4, nhưng chưa tới lượt. SQL người dùng gửi lên vì vậy LUÔN
# ở phương ngữ DuckDB; không có bước `loom_sql.transpile` nào ở đây.
DIALECT = "duckdb"


class SqlSyntaxError(HTTPException):
    """400 kèm dòng/cột cho mỗi lỗi — xem `loom_sql.validate`.

    2c gạch đỏ đúng chỗ trong ô nhập SQL dựa vào chính hình dạng `errors[]`
    này, nên nó không phải một chi tiết trang trí của lỗi 400.
    """

    def __init__(self, errors: list[SqlError]) -> None:
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "the SQL failed to parse",
                "errors": [
                    {"line": e.line, "column": e.column, "message": e.message} for e in errors
                ],
            },
        )


class UnsupportedTableName(HTTPException):
    """400 — quy ước tên bảng chưa mở rộng ở Giai đoạn 2b, xem `_resolve_item_id`."""

    def __init__(self, table: TableRef, reason: str) -> None:
        self.table = table
        super().__init__(status.HTTP_400_BAD_REQUEST, detail=reason)


class ExternalSourceRejected(HTTPException):
    """Query đọc dữ liệu KHÔNG qua catalog — từ chối, và từ chối vì THIẾT KẾ.

    `read_parquet('s3://…')`, `FROM 's3://…/*.parquet'`, hàm bảng: sqlglot phân
    tích cả ba thành `exp.Table`, và `loom_sql.deps.dependencies` tách chúng ra
    `external` thay vì trộn vào danh sách bảng.

    Vì sao chặn thay vì bỏ qua: đường đọc file thô trong `Files/` chưa được xây
    (Giai đoạn 2b sau), nên chưa có gì phân quyền cho nó. Thứ duy nhất đứng chắn
    một `read_parquet('s3://workspace-khac/…')` lúc này là phạm vi của credential
    do Lakekeeper cấp — và một ranh giới duy nhất, không có lớp thứ hai, là chỗ
    một lỗi cấu hình biến thành rò rỉ dữ liệu chéo workspace.

    Chặn ở đây rẻ và nói rõ lý do cho người dùng. Khi đường file thô có mặt, chỗ
    này đổi từ "từ chối" thành "phân quyền theo prefix của workspace".
    """

    def __init__(self, sources: list[str]) -> None:
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "query đọc dữ liệu không qua catalog, chưa hỗ trợ ở giai đoạn này: "
            + ", ".join(sources),
        )


class QueryForbidden(HTTPException):
    """403 — thiếu viewer ở ít nhất một item mà câu SQL chạm tới.

    Cố ý KHÔNG nói item nào: nói ra sẽ xác nhận một bảng cụ thể có tồn tại
    trong lakehouse, đúng lỗ rò rỉ sự tồn tại mà quy tắc 404-trước-403 của
    Giai đoạn 1 sinh ra để chặn (xem `AuthzPort` và `routers/internal.py` bên
    `loom-api`: `null` cho "không tồn tại" và `null` cho "không có quyền" là
    MỘT câu trả lời, không hai).
    """

    def __init__(self) -> None:
        super().__init__(
            status.HTTP_403_FORBIDDEN, detail="you do not have permission to run this query"
        )


class AuthzPort(Protocol):
    """Hợp đồng mà `loom-query` cần từ phía "hỏi quyền".

    `tests/conftest.py` có một bản giả (`FakeAuthz`) hiện thực đúng Protocol
    này mà không cần một `httpx.AsyncClient` hay một `loom-api` thật nào — các
    phép kiểm về THỨ TỰ và về HÀNH VI của `run_gate` không cần biết quyền được
    hỏi qua HTTP thật hay không; điều chúng cần biết là `run_gate` có hỏi đúng
    lúc, đúng id, và có tôn trọng câu trả lời hay không.
    """

    async def roles_for_items(
        self, principal: Principal, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[str, str | None]: ...


@dataclass(frozen=True, slots=True)
class AuthzClient:
    """Bản cài THẬT của `AuthzPort` — gọi `POST {base_url}/authz/items`.

    Không tự đóng `http`: chỗ dựng nó (`main.py`) quyết định ai sở hữu vòng đời
    của `httpx.AsyncClient`, đúng quy tắc "chỉ đóng thứ mình tạo ra" đã dùng ở
    `loom_api.main.create_app`.
    """

    http: httpx.AsyncClient
    base_url: str

    async def roles_for_items(
        self, principal: Principal, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[str, str | None]:
        response = await self.http.post(
            f"{self.base_url}/authz/items",
            json={
                "principal": principal.model_dump(mode="json"),
                "item_ids": [str(item_id) for item_id in item_ids],
            },
        )
        response.raise_for_status()
        roles: dict[str, str | None] = response.json()["roles"]
        return roles


def _resolve_item_id(table: TableRef, lakehouse_id: uuid.UUID) -> uuid.UUID:
    """table -> item id — BẢN THU HẸP của Giai đoạn 2b, xem module docstring.

    Chỉ hỗ trợ tên bảng HAI phần (`namespace.table`), hiểu là nằm trong
    lakehouse của `lakehouse_id` trong request — mọi bảng hai phần hợp lệ vì
    vậy đều trỏ về CÙNG một item id (chính `lakehouse_id`), vì ở Giai đoạn 2b
    một item riêng cho từng bảng chưa tồn tại (`LakehouseDefinition` mới có
    `schema_version`, "Giai đoạn 2 thêm `tables`" — xem
    `loom_core.item_definitions`).

    Tên KHÔNG có namespace (`FROM orders`) và tên BA phần
    (`lakehouse.namespace.table`, tức `table.catalog` VÀ `table.db` đều có
    giá trị — xem `loom_sql.deps.table_deps`) đều bị từ chối bằng 400 "chưa hỗ
    trợ": ba phần cần phân giải `name` của một item `lakehouse` khác sang id
    của nó qua một endpoint internal thứ hai (`/internal/lakehouses/resolve`,
    spec Giai đoạn 2b Task 6) mà task này CỐ Ý chưa dựng — đó là việc của task
    mở tên bảng ba phần / hai lakehouse.
    """
    if table.namespace is None:
        raise UnsupportedTableName(
            table,
            f"table '{table.name}' has no namespace — write it as "
            f"'<namespace>.{table.name}' (unqualified table names are not supported)",
        )
    if "." in table.namespace:
        raise UnsupportedTableName(
            table,
            f"three-part table names ('{table.namespace}.{table.name}') are not "
            "supported yet — use 'namespace.table' within the lakehouse given in the request",
        )
    return lakehouse_id


async def run_gate(
    *,
    sql: str,
    lakehouse_id: uuid.UUID,
    principal: Principal,
    authz: AuthzPort,
) -> tuple[TableRef, ...]:
    """Chạy năm bước đầu của cổng quyền. Ném `HTTPException` nếu bị chặn.

    Trả về danh sách bảng đã kiểm quyền — người gọi (`routers/query.py`) đưa
    thẳng nó cho `runner.py` ở bước 6, để SQL không bị `sqlglot.parse` một lần
    thứ hai cho cùng một câu.
    """
    errors = validate(sql, DIALECT)
    if errors:
        raise SqlSyntaxError(errors)

    deps = dependencies(sql, DIALECT)

    # TRƯỚC khi phân giải và kiểm quyền: một nguồn ngoài catalog không có item
    # nào để hỏi quyền, nên nếu để nó đi tiếp thì nó lặng lẽ không bị kiểm gì.
    if deps.external:
        raise ExternalSourceRejected(deps.external)

    refs = tuple(deps.tables)

    # `dict.fromkeys` khử trùng lặp nhưng GIỮ hình dạng "một id cho mỗi bảng"
    # thay vì sớm rút gọn về một tập hợp: trong task này mọi bảng hợp lệ đều
    # trỏ về CÙNG `lakehouse_id`, nhưng viết vòng lặp theo từng `ref` (thay vì
    # gọi `_resolve_item_id` một lần rồi coi như xong) là để Task mở hai
    # lakehouse chỉ cần đổi `_resolve_item_id`, không đổi hàm này.
    item_ids = tuple(dict.fromkeys(_resolve_item_id(ref, lakehouse_id) for ref in refs))

    roles = await authz.roles_for_items(principal, item_ids)

    # "Thiếu viewer" ĐÚNG BẰNG "role trả về là None": `loom_core.roles.
    # ACTION_MATRIX` xếp `item.read` vào tập quyền THẤP NHẤT (viewer) và mọi
    # vai trò cao hơn là SUPERSET của nó, nên "có bất kỳ vai trò nào" và "có ít
    # nhất viewer" là một mệnh đề — không cần so sánh với `Role.viewer` ở đây,
    # và làm vậy sẽ là tính lại một phần luật mà `loom_api.permissions` đã có.
    missing = [item_id for item_id in item_ids if roles.get(str(item_id)) is None]
    if missing:
        raise QueryForbidden

    return refs
