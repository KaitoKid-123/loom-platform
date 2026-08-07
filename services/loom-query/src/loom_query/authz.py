"""Cổng quyền — phần quan trọng nhất của `loom-query`, chạy ĐỒNG BỘ trong POST.

Thứ tự dưới đây LÀ đặc tả, không phải chi tiết cài đặt (spec Giai đoạn 2b):

    1. sqlkit.validate(sql)      -> lỗi cú pháp -> 400 kèm dòng/cột
    2. sqlkit.table_deps(sql)    -> mọi bảng, kể cả trong CTE và subquery
    3. bảng -> item id           -> xem `_resolve_item_ids`
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

**Bước 3 giờ có hai nhánh** (tên bảng ba phần / hai lakehouse, xem
`_resolve_item_ids`): bảng hai phần trỏ thẳng về `lakehouse_id` của request,
bảng ba phần cần dịch `name` của MỘT lakehouse khác sang id qua
`LakehouseResolver.resolve_lakehouses` (`POST /internal/lakehouses/resolve`
bên `loom-api`) trước khi bước 4 hỏi quyền trên id đó. `LakehouseResolver` là
một Protocol THỨ HAI, tách khỏi `AuthzPort`, vì hai câu hỏi khác hẳn nhau: một
cái hỏi "quyền", một cái chỉ dịch tên — đúng cách `/internal/lakehouses/
resolve` tách khỏi `/internal/authz/items` bên `loom-api` (xem docstring
`routers/internal.py` ở đó). `AuthzClient` cài CẢ HAI Protocol trên cùng một
`base_url`, vì cả hai đều là chuyện của `loom-api`; `FakeAuthz`
(`tests/conftest.py`) cũng vậy, để test không phải tiêm hai đối tượng giả cho
một thứ về bản chất là "hỏi loom-api".
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
    """400 — tên bảng KHÔNG có namespace (`FROM orders`), xem `_resolve_item_ids`.

    Đây là trường hợp DUY NHẤT còn bị từ chối ở bước phân giải: hai phần
    (`namespace.table`) và ba phần (`lakehouse.namespace.table`) đều được hỗ
    trợ. `loom-query` cố tình không đoán namespace hộ người dùng.
    """

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


class LakehouseResolver(Protocol):
    """Hợp đồng "dịch tên", TÁCH khỏi `AuthzPort` — xem docstring module.

    Đây KHÔNG phải một cổng quyền: `resolve_lakehouses` không nói gì về việc
    principal có đọc được lakehouse hay không, chỉ nói tên đó tồn tại (và
    đang active) hay không. `run_gate` là nơi biến câu trả lời `None` của
    Protocol này thành 403 — chỗ hỏi (đây) và chỗ quyết định "cho qua hay
    không" (`run_gate`) tách nhau, cùng lý do `AuthzPort` tách khỏi luật RBAC.
    """

    async def resolve_lakehouses(
        self, workspace_id: uuid.UUID, names: tuple[str, ...]
    ) -> dict[str, uuid.UUID | None]: ...


@dataclass(frozen=True, slots=True)
class AuthzClient:
    """Bản cài THẬT của CẢ `AuthzPort` LẪN `LakehouseResolver` — gọi
    `POST {base_url}/authz/items` và `POST {base_url}/lakehouses/resolve`.

    Một class cho cả hai Protocol vì cả hai đều là "hỏi `loom-api` qua cùng
    `base_url` nội bộ" — không có lý do tách hai `httpx.AsyncClient`/hai cấu
    hình chỉ để giữ ranh giới kiểu tồn tại trên giấy.

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

    async def resolve_lakehouses(
        self, workspace_id: uuid.UUID, names: tuple[str, ...]
    ) -> dict[str, uuid.UUID | None]:
        response = await self.http.post(
            f"{self.base_url}/lakehouses/resolve",
            json={"workspace_id": str(workspace_id), "names": list(names)},
        )
        response.raise_for_status()
        raw_ids: dict[str, str | None] = response.json()["ids"]
        return {
            name: (uuid.UUID(value) if value is not None else None)
            for name, value in raw_ids.items()
        }


def _lakehouse_name_of(namespace: str) -> str | None:
    """`name` của lakehouse nêu trong một tên bảng BA phần, hoặc `None` nếu
    `namespace` chỉ có một phần (tên bảng HAI phần, `namespace.table`).

    `namespace` tới từ `TableRef.namespace` — `loom_sql.deps.dependencies` nối
    `table.catalog` và `table.db` bằng một dấu chấm khi cả hai có giá trị.
    sqlglot không cho một `exp.Table` nhiều hơn ba phần (`catalog.db.table`),
    nên `namespace` ở đây mang TỐI ĐA một dấu chấm — `partition` (tách ở dấu
    chấm ĐẦU TIÊN) là đủ, không cần đếm số phần.
    """
    lakehouse_name, dot, _rest = namespace.partition(".")
    return lakehouse_name if dot else None


async def _resolve_item_ids(
    refs: tuple[TableRef, ...],
    *,
    lakehouse_id: uuid.UUID,
    workspace_id: uuid.UUID,
    resolver: LakehouseResolver,
) -> tuple[uuid.UUID, ...]:
    """Mọi `TableRef` trong `refs` -> id item `lakehouse` tương ứng — quy ước
    đặt tên bảng đã chốt (xem module docstring và spec Giai đoạn 2b Task 6/7):

    - `namespace.table` (HAI phần): trỏ THẲNG về `lakehouse_id` của request —
      không có gì để hỏi, mọi bảng hai phần hợp lệ trỏ về CÙNG một id.
    - `lakehouse.namespace.table` (BA phần): `lakehouse` là `name` của một item
      `type='lakehouse'` KHÁC, cùng workspace với `lakehouse_id` — cần dịch
      sang id qua `resolver.resolve_lakehouses`.

    **MỘT lần gọi `resolver.resolve_lakehouses` cho TOÀN BỘ danh sách tên**,
    không phải một lần cho mỗi bảng: gom hết tên lakehouse xuất hiện trong
    `refs` (khử trùng lặp) rồi hỏi một lượt — một `JOIN` năm bảng ở CÙNG một
    lakehouse thứ hai không được sinh năm round trip HTTP. Đây cũng là lý do
    hàm này nhận `refs` là một khối thay vì được gọi lặp lại cho từng `ref` như
    `_resolve_item_id` (bản cũ, một-bảng) đã làm.

    Không phân giải được một tên (`resolver` trả `None` cho nó) ném THẲNG
    `QueryForbidden` — KHÔNG phải một lỗi 404 riêng cho "tên sai". "Tên
    lakehouse không tồn tại" và "tên lakehouse tồn tại nhưng principal không
    có quyền" phải là MỘT câu trả lời từ phía người dùng cuối (403, cùng thông
    điệp) — đúng quy tắc 404-trước-403 mà `QueryForbidden` đã ghi trong
    docstring của chính nó, áp dụng ở đây cho danh mục lakehouse thay vì cho
    một item bên trong nó.
    """
    namespaces: list[str] = []
    for ref in refs:
        if ref.namespace is None:
            raise UnsupportedTableName(
                ref,
                f"table '{ref.name}' has no namespace — write it as "
                f"'<namespace>.{ref.name}' (unqualified table names are not supported)",
            )
        namespaces.append(ref.namespace)

    cross_lakehouse_names = tuple(
        dict.fromkeys(name for ns in namespaces if (name := _lakehouse_name_of(ns)) is not None)
    )
    resolved = (
        await resolver.resolve_lakehouses(workspace_id, cross_lakehouse_names)
        if cross_lakehouse_names
        else {}
    )

    item_ids: list[uuid.UUID] = []
    for ns in namespaces:
        name = _lakehouse_name_of(ns)
        if name is None:
            item_ids.append(lakehouse_id)
            continue
        other_id = resolved.get(name)
        if other_id is None:
            raise QueryForbidden
        item_ids.append(other_id)
    return tuple(dict.fromkeys(item_ids))


async def run_gate(
    *,
    sql: str,
    lakehouse_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal: Principal,
    authz: AuthzPort,
    resolver: LakehouseResolver,
) -> tuple[TableRef, ...]:
    """Chạy năm bước đầu của cổng quyền. Ném `HTTPException` nếu bị chặn.

    `workspace_id` là workspace CHỨA `lakehouse_id` của request — cần nó để
    phân giải tên bảng ba phần (`_resolve_item_ids`) đúng phạm vi: tên lakehouse
    là duy nhất trong MỘT workspace, không phải toàn hệ thống, nên tìm nó mà
    không giới hạn workspace là tìm ở phạm vi sai. `routers/query.py` lấy giá
    trị này từ `QueryCreate.workspace_id` — xem docstring trường đó cho lý do
    nó tới qua thân request thay vì `loom-query` tự tra cứu (không có database).

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

    # `_resolve_item_ids` tự khử trùng lặp NHƯNG kiểm quyền trên TOÀN BỘ id thu
    # được — không rút gọn xuống "bảng đầu tiên" ở đây hay bên trong nó. Một
    # `JOIN` hai lakehouse phải hỏi quyền trên CẢ HAI id, xem
    # `tests/test_query_authz_gate.py` cho phép kiểm canh đúng lỗi này.
    item_ids = await _resolve_item_ids(
        refs, lakehouse_id=lakehouse_id, workspace_id=workspace_id, resolver=resolver
    )

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
