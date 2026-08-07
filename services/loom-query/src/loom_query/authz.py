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

from loom_core.roles import Action, Role, allows
from loom_core.schemas import Principal
from loom_query.files import UnsafeFilesPath, validate_files_paths
from loom_sql import SqlError, TableRef, validate
from loom_sql.deps import FILE_READ_FUNCTIONS, dependencies

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

    **CẬP NHẬT — Task 13 (Giai đoạn 2b) mở một khe hẹp trong lệnh cấm này:**
    `read_parquet`/`read_csv` trỏ vào `Files/` của ĐÚNG lakehouse trong request
    giờ ĐƯỢC PHỤC VỤ (`_check_files_access` bên dưới, dùng `loom_query.files`),
    thay vì lệnh cấm rơi vào MỌI thứ trong `external`. Vì sao vẫn còn class
    này thay vì xoá hẳn: mọi thứ khác trong `external` — path trần
    (`FROM 's3://…'`), `range`/`generate_series` (không đọc dữ liệu từ đâu cả,
    quyết định KHÔNG mở khoá — xem báo cáo hoàn tất Task 13), bốn hàm còn lại
    của `_KNOWN_READERS` bên `loom_sql.deps` (`read_csv_auto`, `read_json`,
    `read_json_auto`, `parquet_scan`) — vẫn bị chặn NGUYÊN VẸN như trước, và
    một `read_parquet('s3://workspace-khac/…')` (path KHÔNG an toàn) cũng vậy
    (dù giờ raise qua `InvalidFilesPath`, không phải class này — xem đó).

    Vì sao chặn thay vì bỏ qua, cho PHẦN CÒN LẠI: chưa có gì phân quyền cho
    chúng. Thứ duy nhất đứng chắn một `FROM 's3://workspace-khac/…'` lúc này là
    phạm vi của credential do Lakekeeper cấp — và một ranh giới duy nhất, không
    có lớp thứ hai, là chỗ một lỗi cấu hình biến thành rò rỉ dữ liệu chéo
    workspace. Chặn ở đây rẻ và nói rõ lý do cho người dùng.
    """

    def __init__(self, sources: list[str]) -> None:
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "query đọc dữ liệu không qua catalog, chưa hỗ trợ ở giai đoạn này: "
            + ", ".join(sources),
        )


class InvalidFilesPath(HTTPException):
    """400 — một `read_parquet`/`read_csv` trỏ ra ngoài `Files/` của lakehouse
    (tuyệt đối, có scheme, thoát prefix...), hoặc không dùng literal chuỗi làm
    path — xem `loom_query.files.UnsafeFilesPath`.

    KHÁC `ExternalSourceRejected` dù cùng 400: thông điệp ở đây nói RÕ lý do
    (thoát prefix, tuyệt đối...), vì đọc `Files/` bằng `read_parquet`/
    `read_csv` LÀ tính năng được hỗ trợ ở Giai đoạn 2b (Task 13) — chỉ ĐÚNG
    path này không an toàn, không phải "loom-query chưa biết đọc file thô".
    """

    def __init__(self, reason: str) -> None:
        super().__init__(status.HTTP_400_BAD_REQUEST, f"invalid path under 'Files/': {reason}")


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


def lakehouse_alias_of(namespace: str) -> str | None:
    """`name` của lakehouse nêu trong một tên bảng BA phần, hoặc `None` nếu
    `namespace` chỉ có một phần (tên bảng HAI phần, `namespace.table`).

    KHÔNG còn là chi tiết riêng của module này (đã bỏ dấu `_` đầu tên): `xem
    `runner.py` cho lý do nó cần biết chính điều này để quyết định CATALOG
    DuckDB nào phải mở cho một bảng — ATTACH một catalog mới mang đúng tên
    lakehouse, hay dùng catalog mặc định của connection.

    `namespace` tới từ `TableRef.namespace` — `loom_sql.deps.dependencies` nối
    `table.catalog` và `table.db` bằng một dấu chấm khi cả hai có giá trị.
    sqlglot không cho một `exp.Table` nhiều hơn ba phần (`catalog.db.table`),
    nên `namespace` ở đây mang TỐI ĐA một dấu chấm — `partition` (tách ở dấu
    chấm ĐẦU TIÊN) là đủ, không cần đếm số phần.
    """
    lakehouse_name, dot, _rest = namespace.partition(".")
    return lakehouse_name if dot else None


def strip_lakehouse_alias(namespace: str) -> str:
    """Namespace THẬT bên trong lakehouse sở hữu bảng — bỏ tiền tố tên
    lakehouse nếu `namespace` là ba phần; giữ nguyên nếu đã là hai phần (không
    có gì để bỏ).

    Cặp với `lakehouse_alias_of`. `runner.py` dùng hàm này để biết
    namespace/tên THẬT cần đưa cho `Lakehouse.scan()` trên catalog Iceberg của
    lakehouse sở hữu bảng — catalog đó không biết gì về cái tên (alias) mà câu
    SQL của người dùng dùng để TRỎ tới nó, nó chỉ biết namespace THẬT của
    chính nó.
    """
    _prefix, dot, rest = namespace.partition(".")
    return rest if dot else namespace


@dataclass(frozen=True, slots=True)
class ResolvedTable:
    """Một bảng đã qua bước phân giải, cùng id lakehouse SỞ HỮU nó.

    `runner.py` cần vế `lakehouse_id` để mở ĐÚNG catalog Iceberg cho bảng này
    (xem docstring `runner._run_sync`): với bảng hai phần, đó luôn là
    `lakehouse_id` của chính request; với bảng ba phần, đó là id đã phân giải
    từ tên lakehouse khác qua `resolver.resolve_lakehouses`. `ref` giữ nguyên
    `namespace`/`name` NHƯ CÂU SQL VIẾT (kể cả tiền tố alias ba phần) — đó là
    thứ `runner.py` cần để tạo lại đúng tên bảng trong DuckDB, phân biệt với
    tên THẬT trên catalog Iceberg (`strip_lakehouse_alias`).

    **`is_read`/`is_write` (Giai đoạn 2c, CTAS).** Trực tiếp từ
    `loom_sql.deps.Dependencies.reads`/`.writes` — `runner._run_sync` cần biết
    vế này để quyết định có `Lakehouse.scan()` bảng hay không (chỉ ĐỌC mới
    quét — một đích CTAS chưa tồn tại thì KHÔNG có gì để quét) và có tính vào
    trần byte quét hay không (cùng lý do). Một bảng vừa đọc vừa ghi (`INSERT
    INTO t SELECT * FROM t`) mang CẢ HAI cờ `True`. Mặc định `is_read=True,
    is_write=False` — đúng hành vi DUY NHẤT tồn tại trước Giai đoạn 2c (mọi
    bảng đều được quét), nên ba bài test gọi thẳng `runner.execute` bằng
    `ResolvedTable(ref=..., lakehouse_id=...)` không cần sửa.
    """

    ref: TableRef
    lakehouse_id: uuid.UUID
    is_read: bool = True
    is_write: bool = False


async def _resolve_tables(
    refs: tuple[TableRef, ...],
    *,
    reads: frozenset[TableRef],
    writes: frozenset[TableRef],
    lakehouse_id: uuid.UUID,
    workspace_id: uuid.UUID,
    resolver: LakehouseResolver,
) -> tuple[ResolvedTable, ...]:
    """Mọi `TableRef` trong `refs` -> `ResolvedTable` (bảng + id lakehouse sở
    hữu nó + cờ đọc/ghi) — quy ước đặt tên bảng đã chốt (xem module docstring
    và spec Giai đoạn 2b Task 6/7):

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

    **KHÔNG tự quyết "có cần hỏi quyền trên `lakehouse_id` hay không" ở đây.**
    Hàm này chỉ trả về bảng nào thuộc lakehouse nào — người gọi (`run_gate`)
    mới là chỗ quyết định tập id cần hỏi quyền, và `lakehouse_id` PHẢI có mặt
    trong tập đó VÔ ĐIỀU KIỆN dù `refs` ở đây có bảng hai phần hay không (xem
    comment ở `run_gate`) — đó là lỗi bảo mật đã sửa: catalog của `lakehouse_id`
    vẫn có thể bị `runner` mở dù không có bảng hai phần nào trỏ tới nó.
    """
    checked: list[tuple[TableRef, str]] = []
    for ref in refs:
        if ref.namespace is None:
            raise UnsupportedTableName(
                ref,
                f"table '{ref.name}' has no namespace — write it as "
                f"'<namespace>.{ref.name}' (unqualified table names are not supported)",
            )
        checked.append((ref, ref.namespace))

    cross_lakehouse_names = tuple(
        dict.fromkeys(name for _, ns in checked if (name := lakehouse_alias_of(ns)) is not None)
    )
    resolved = (
        await resolver.resolve_lakehouses(workspace_id, cross_lakehouse_names)
        if cross_lakehouse_names
        else {}
    )

    resolved_tables: list[ResolvedTable] = []
    for ref, ns in checked:
        alias = lakehouse_alias_of(ns)
        is_read = ref in reads
        is_write = ref in writes
        if alias is None:
            resolved_tables.append(
                ResolvedTable(
                    ref=ref, lakehouse_id=lakehouse_id, is_read=is_read, is_write=is_write
                )
            )
            continue
        other_id = resolved.get(alias)
        if other_id is None:
            raise QueryForbidden
        resolved_tables.append(
            ResolvedTable(ref=ref, lakehouse_id=other_id, is_read=is_read, is_write=is_write)
        )
    return tuple(resolved_tables)


def _check_files_access(sql: str, external: list[str]) -> None:
    """Quyết định phần CÒN LẠI của lệnh cấm `ExternalSourceRejected`, sau khi
    Task 13 mở đúng một khe hẹp — đọc docstring của class đó trước khi sửa
    hàm này.

    `external` (nhãn từ `dependencies()`, xem `loom_sql.deps`) khớp
    `sql_name()` mà sqlglot trả cho MỘT lời gọi hàm — CHỮ HOA, có gạch dưới
    (`"READ_PARQUET"`), hoặc chính path trần nếu đó là `FROM 's3://…'` (không
    khớp gì trong `FILE_READ_FUNCTIONS`, luôn bị chặn). So sánh bằng
    `.lower()` để khớp `FILE_READ_FUNCTIONS` (chữ thường, xem `loom_sql.deps`)
    — MỘT nguồn sự thật cho "hai hàm nào được phục vụ", không phải một bản
    sao chữ hoa tự chép tay ở đây.

    BẤT KỲ nhãn nào KHÔNG khớp `FILE_READ_FUNCTIONS` giữ NGUYÊN hành vi CŨ —
    `ExternalSourceRejected` cho TOÀN BỘ danh sách, không phân biệt: đây là
    bằng chứng "lệnh cấm cũ còn nguyên" (chứng minh đỏ 5 của Task 13). Chỉ khi
    MỌI nhãn đều thuộc `FILE_READ_FUNCTIONS`, hàm này mới hỏi tiếp
    `loom_query.files.validate_files_paths` — trọng tài THẬT về an toàn của
    TỪNG path — và biến một `UnsafeFilesPath` (nếu có) thành `InvalidFilesPath`
    (400, nói rõ lý do), KHÁC thông điệp "chưa hỗ trợ" chung chung của
    `ExternalSourceRejected`: đọc `Files/` LÀ tính năng được hỗ trợ, chỉ path
    cụ thể này không an toàn.
    """
    if any(label.lower() not in FILE_READ_FUNCTIONS for label in external):
        raise ExternalSourceRejected(external)
    try:
        validate_files_paths(sql, DIALECT)
    except UnsafeFilesPath as exc:
        raise InvalidFilesPath(str(exc)) from exc


async def run_gate(
    *,
    sql: str,
    lakehouse_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal: Principal,
    authz: AuthzPort,
    resolver: LakehouseResolver,
) -> tuple[ResolvedTable, ...]:
    """Chạy năm bước đầu của cổng quyền. Ném `HTTPException` nếu bị chặn.

    `workspace_id` là workspace CHỨA `lakehouse_id` của request — cần nó để
    phân giải tên bảng ba phần (`_resolve_tables`) đúng phạm vi: tên lakehouse
    là duy nhất trong MỘT workspace, không phải toàn hệ thống, nên tìm nó mà
    không giới hạn workspace là tìm ở phạm vi sai. `routers/query.py` lấy giá
    trị này từ `QueryCreate.workspace_id` — xem docstring trường đó cho lý do
    nó tới qua thân request thay vì `loom-query` tự tra cứu (không có database).

    Trả về danh sách bảng đã kiểm quyền, MỖI bảng kèm id lakehouse sở hữu nó
    (`ResolvedTable`) — người gọi (`routers/query.py`) đưa thẳng nó cho
    `runner.py` ở bước 6, để SQL không bị `sqlglot.parse` một lần thứ hai cho
    cùng một câu, VÀ để `runner.py` biết mở catalog Iceberg nào cho bảng nào
    (xem docstring `runner._run_sync`).
    """
    errors = validate(sql, DIALECT)
    if errors:
        raise SqlSyntaxError(errors)

    deps = dependencies(sql, DIALECT)

    # TRƯỚC khi phân giải và kiểm quyền: một nguồn ngoài catalog không có item
    # nào để hỏi quyền, nên nếu để nó đi tiếp thì nó lặng lẽ không bị kiểm gì.
    # `_check_files_access` mở ĐÚNG MỘT khe hẹp trong lệnh cấm đó (Task 13) —
    # xem docstring của nó và của `ExternalSourceRejected`.
    if deps.external:
        _check_files_access(sql, deps.external)

    refs = tuple(deps.tables)
    reads = frozenset(deps.reads)
    writes = frozenset(deps.writes)

    # `_resolve_tables` tự khử trùng lặp NHƯNG kiểm quyền trên TOÀN BỘ id thu
    # được — không rút gọn xuống "bảng đầu tiên" ở đây hay bên trong nó. Một
    # `JOIN` hai lakehouse phải hỏi quyền trên CẢ HAI id, xem
    # `tests/test_query_authz_gate.py` cho phép kiểm canh đúng lỗi này.
    resolved_tables = await _resolve_tables(
        refs,
        reads=reads,
        writes=writes,
        lakehouse_id=lakehouse_id,
        workspace_id=workspace_id,
        resolver=resolver,
    )

    # BẤT BIẾN BẮT BUỘC, đã từng bị vi phạm: **catalog mà `runner` mở PHẢI nằm
    # trong tập đã được cấp quyền.** `runner._run_sync` mở catalog cho MỌI
    # lakehouse xuất hiện trong `resolved_tables` — VÀ với `lakehouse_id` của
    # chính request, nó luôn coi đây là catalog "nhà" của mọi bảng hai phần dù
    # câu SQL có bảng hai phần nào hay không (bảng hai phần trỏ THẲNG về nó,
    # xem `_resolve_tables`). `lakehouse_id` vì vậy PHẢI luôn nằm trong tập id
    # được hỏi quyền dưới đây — kể cả khi TOÀN BỘ bảng trong câu SQL là tên ba
    # phần trỏ tới lakehouse KHÁC. Thiếu dòng `lakehouse_id` này: một câu SQL
    # toàn-ba-phần trỏ tới lakehouse B (người dùng CÓ quyền) trong khi
    # `lakehouse_id=A` của chính request KHÔNG có quyền vẫn lọt qua cổng —
    # `resolved_tables` khi đó không chứa `A` ở đâu cả, nên `A` không bao giờ
    # được hỏi quyền, dù runner vẫn mở catalog của A một cách vô điều kiện.
    item_ids = tuple(
        dict.fromkeys((lakehouse_id, *(table.lakehouse_id for table in resolved_tables)))
    )

    roles = await authz.roles_for_items(principal, item_ids)

    # Lakehouse nào có ÍT NHẤT một đích ghi (CTAS/`INSERT ... SELECT`) đòi
    # `item.update` (contributor trở lên) trên CHÍNH lakehouse đó — không phải
    # `item.read` (viewer) như đọc. Một lakehouse có thể vừa xuất hiện ở đây
    # vừa KHÔNG (một `JOIN` ghi vào lakehouse A trong khi chỉ đọc lakehouse B):
    # yêu cầu quyền tính RIÊNG cho từng lakehouse, không phải một mức chung cho
    # cả câu SQL.
    write_lakehouse_ids = {table.lakehouse_id for table in resolved_tables if table.is_write}

    # "Thiếu viewer" KHÔNG còn đúng bằng "role trả về là None" kể từ khi GHI
    # tồn tại — mệnh đề cũ (đã ghi ở đây trước Giai đoạn 2c) chỉ đúng cho ĐỌC:
    # `loom_core.roles.ACTION_MATRIX` xếp `item.read` vào tập quyền THẤP NHẤT
    # (viewer) và mọi vai trò cao hơn là SUPERSET của nó, nên với đọc, "có bất
    # kỳ vai trò nào" và "có ít nhất viewer" vẫn là một mệnh đề. Nhưng
    # `item.update` (GHI) chỉ nằm trong `contributor` trở lên — một `viewer`
    # CÓ vai trò (`roles.get(...)` khác `None`) nhưng KHÔNG đủ quyền ghi, nên
    # phải gọi thẳng `allows()` cho từng lakehouse thay vì chỉ kiểm `is None`.
    # Dùng `allows()` (từ `loom_core.roles`, nguồn quy tắc DUY NHẤT — xem module
    # docstring) chứ không tự so `role >= contributor` bằng tay: một phép so
    # viết tay ở đây là đường tính luật RBAC thứ ba, đúng thứ mà việc gom luật
    # vào `loom_core.roles` ở Giai đoạn 1 tồn tại để chặn.
    insufficient = []
    for item_id in item_ids:
        role_name = roles.get(str(item_id))
        if role_name is None:
            insufficient.append(item_id)
            continue
        required = Action.item_update if item_id in write_lakehouse_ids else Action.item_read
        if not allows(Role[role_name], required):
            insufficient.append(item_id)
    if insufficient:
        raise QueryForbidden

    return resolved_tables
