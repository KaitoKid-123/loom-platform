"""Chạy SQL đã qua cổng quyền, trên DuckDB, đọc bảng qua `Lakehouse.scan()`.

Đây là bước 6 của đường một query — chỉ tới lượt SAU khi `authz.run_gate` đã
xong (xem module docstring của `authz.py`). Hàm ở đây không tự hỏi quyền, và
không được phép: gọi nó trực tiếp mà bỏ qua `run_gate` là đúng lỗ mà "chứng
minh đỏ 2" của Task 6 chặn.

DuckDB CHẶN: `.execute()`/`.sql()` là hàm đồng bộ của thư viện C++, nên chạy
thẳng trong một coroutine sẽ khoá nguyên event loop tới khi câu SQL xong — mọi
`GET`/`DELETE` của MỌI query khác (kể cả query đang chạy trên node khác — không
áp dụng ở đây vì mỗi pod một event loop riêng, nhưng ngay trong MỘT pod cũng đã
đủ tệ) sẽ treo theo. `asyncio.to_thread` đẩy phần chặn ra một thread riêng.

`memory_limit=256MB` và `threads=2` GHIM CỨNG, không phải cấu hình đọc từ
`Settings`: đây là hai con số đã ĐO ở Giai đoạn 2a (RSS đỉnh 348 Mi trong
container 384Mi ứng với `threads=2` — không hơn — để không OOM trên một node
nhiều core). Ba giới hạn còn lại (byte quét, thời gian, số dòng) đọc từ
`Settings` (Task 8); hai giá trị này thì không, vì chúng là cấu hình khởi tạo
DuckDB đến từ một phép đo, không phải một tham số vận hành.

**Huỷ/timeout đều đi qua CÙNG một cơ chế: `connection.interrupt()`.** Đã kiểm
bằng thực nghiệm (xem báo cáo Task 9) rằng `asyncio.Task.cancel()` trên một
task đang `await asyncio.to_thread(...)` KHÔNG dừng được thread OS bên dưới —
nó chỉ từ bỏ việc CHỜ, thread nền vẫn chạy hết. `duckdb.DuckDBPyConnection.
interrupt()` thì khác: gọi được từ MỘT thread khác trong khi thread kia đang
chặn trong DuckDB, và DuckDB tự kiểm cờ ngắt đó định kỳ, ném
`duckdb.InterruptException` gần như ngay lập tức — kể cả sau khi bị ngắt,
connection vẫn dùng được (đã kiểm ở Giai đoạn 2a,
`packages/icebergkit/tests/test_duckdb_memory.py`). `execute()` dưới đây publish
connection ra ngay khi nó vừa được tạo — TRƯỚC `build_catalog`/bất kỳ câu SQL
nào — để cửa sổ "có việc đang chạy mà chưa huỷ được" hẹp nhất có thể.

**Một catalog Iceberg riêng cho MỖI lakehouse mà câu SQL chạm tới, không một
catalog chung cho tất cả.** `run_gate` (`authz.py`) trả về `ResolvedTable` —
mỗi bảng kèm ĐÚNG id lakehouse sở hữu nó — chính xác để `_run_sync` mở đúng
catalog cho đúng bảng. Trộn chung một catalog cho mọi bảng (bản trước bản sửa
này) có hai lỗi, một bảo mật một đúng đắn:

- **Bảo mật:** catalog mà `_run_sync` mở PHẢI nằm trong tập đã được cấp quyền
  (xem comment ở `run_gate`) — mở catalog của `lakehouse_id` cho một bảng
  THẬT RA thuộc lakehouse khác là mở một catalog không được cấp quyền cho
  đúng bảng đó.
- **Đúng đắn, im lặng:** nếu hai lakehouse có `namespace.table` TRÙNG TÊN (ví
  dụ cả hai đều có `finance.reports`), dồn chúng vào MỘT catalog DuckDB khiến
  view của lakehouse chèn sau ĐÈ lên view của lakehouse chèn trước — câu SQL
  vẫn CHẠY ĐƯỢC, không lỗi, nhưng một trong hai lakehouse đọc nhầm dữ liệu của
  lakehouse kia. Không ném ngoại lệ nào để báo — đây là loại lỗi tệ nhất.

Giải: `ATTACH ':memory:' AS <alias>` một catalog DuckDB THẬT cho mỗi tên
lakehouse (alias) xuất hiện trong một tên bảng BA phần, xem `_view_target`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import duckdb

# pyarrow không phát hành `py.typed` (khác pyiceberg/duckdb — xem
# `[[tool.mypy.overrides]]` ở pyproject.toml gốc, và cùng lý do đã ghi ở
# `loom_iceberg.lakehouse`): `type: ignore` cục bộ ở đây thay vì thêm pyarrow
# vào danh sách bỏ qua toàn workspace.
import pyarrow as pa  # type: ignore[import-untyped]

from loom_iceberg import Lakehouse, build_catalog
from loom_query.authz import DIALECT, ResolvedTable, lakehouse_alias_of, strip_lakehouse_alias
from loom_query.config import Settings
from loom_query.files import resolve_files_query
from loom_query.limits import check_scan_bytes, truncate_table
from loom_query.schemas import ColumnOut
from loom_query.store import QueryStore
from loom_sql import TableRef
from loom_storage import StorageCredentials

_MEMORY_LIMIT = "256MB"
_THREADS = 2

# MinIO bỏ qua region trong thực tế (đã kiểm bằng thực nghiệm — xem báo cáo
# hoàn tất Task 13), nhưng `CREATE SECRET ... (TYPE s3, ...)` của DuckDB đòi
# một giá trị. Khớp mặc định của `MinioStsProvider` (`packages/storagekit`) —
# một hằng số, không phải một trường `Settings` mới, vì không có gì để cấu
# hình: giá trị này KHÔNG đổi khi MinIO chuyển ra VPS riêng (spec Giai đoạn 2
# mục 2.0 — đó là đổi endpoint/TLS, không đổi region).
_FILES_S3_REGION = "us-east-1"

# Tên định danh của SECRET nạp vào MỖI connection DuckDB — cục bộ trong phạm vi
# MỘT connection (`:memory:`, một cái mới cho mỗi query, xem `_run_sync`), nên
# một tên cố định không va chạm giữa các query chạy đồng thời (mỗi query một
# connection riêng, một process riêng — không có gì dùng chung).
_FILES_SECRET_NAME = "loom_files_secret"  # noqa: S105 — tên định danh, không phải mật khẩu


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: list[ColumnOut]
    rows: list[list[Any]]
    truncated: bool
    row_count: int


def _quote_ident(name: str) -> str:
    """Trích dẫn một định danh DuckDB, thoát dấu ngoặc kép bằng cách nhân đôi.

    `namespace`/`name` tới từ `TableRef` — đã qua `sqlglot.parse`, nên không
    phải input người dùng chưa được kiểm dạng chuỗi thô — nhưng dựng DDL bằng
    f-string vẫn không được PHÉP giả định chúng sạch; trích dẫn tường minh ở
    đây rẻ hơn nhiều so với debug một namespace tình cờ mang dấu `"`.
    """
    return '"' + name.replace('"', '""') + '"'


def _quote_secret_value(value: str) -> str:
    """Thoát dấu `'` trong một giá trị chèn vào `CREATE SECRET` bằng f-string —
    cùng lý do và cùng cách `_quote_ident` thoát dấu `"` cho định danh: giá trị
    tới từ STS (AWS/MinIO) trong thực tế không bao giờ chứa dấu `'`, nhưng
    dựng DDL bằng f-string vẫn không được PHÉP giả định vậy.
    """
    return value.replace("'", "''")


def _install_files_secret(
    connection: duckdb.DuckDBPyConnection,
    *,
    storage: StorageCredentials,
    workspace_id: uuid.UUID,
    settings: Settings,
) -> None:
    """Nạp credential STS hẹp-theo-WORKSPACE (`MinioStsProvider.for_workspace`,
    `packages/storagekit`) vào DuckDB qua httpfs — LỚP BẢO VỆ THỨ HAI của
    đường Files/, xem module docstring `loom_query.files` cho lý do nó KHÔNG
    thay được lớp MỘT (`safe_relative_path`, đã chạy trong `authz.run_gate`
    TRƯỚC khi tới đây, ĐỒNG BỘ, chưa từng chạm S3): credential ở đây hợp lệ
    cho CẢ workspace, RỘNG HƠN một lakehouse.

    `LOAD` — KHÔNG `INSTALL` — httpfs ở đây: extension đã được NẠP SẴN lúc
    build image (xem `Dockerfile`) vì container chạy `readOnlyRootFilesystem:
    true` (`query-deployment.yaml`) — `INSTALL` lúc runtime sẽ cố ghi file
    extension vào một filesystem chỉ đọc. Đã kiểm bằng thực nghiệm: `LOAD` một
    extension ĐÃ có sẵn trong `extension_directory` mặc định (`$HOME/.duckdb/
    extensions/v<version>/<platform>/`) không cần ghi gì và không gọi mạng,
    chạy được trên một `$HOME` chỉ có quyền đọc.

    CHỈ gọi hàm này khi CHẮC CHẮN có ít nhất một `read_parquet`/`read_csv` cần
    phục vụ (`FilesQuery.has_file_reads`, xem `_run_sync`) — một round trip STS
    (`storage.for_workspace`, gọi mạng THẬT tới MinIO) cho MỌI query, kể cả
    query chỉ đọc catalog, là phí vô ích.
    """
    connection.execute("LOAD httpfs")
    credentials = storage.for_workspace(workspace_id)
    endpoint = settings.s3_endpoint.removeprefix("https://").removeprefix("http://")
    use_ssl = "true" if settings.s3_endpoint.startswith("https://") else "false"
    # Dựng DDL bằng f-string: mọi giá trị chèn vào đây hoặc là hằng số cục bộ
    # (`_FILES_SECRET_NAME`/`_FILES_S3_REGION`/`URL_STYLE`) hoặc đã qua
    # `_quote_secret_value` (thoát dấu `'`) — không đoạn nào mang một chuỗi
    # SQL do người dùng cuối tự do gõ vào (path `Files/…` không xuất hiện ở
    # đây, nó nằm trong CHÍNH câu SQL, xử lý riêng ở `resolve_files_query`).
    connection.execute(
        f"""
        CREATE SECRET {_FILES_SECRET_NAME} (
            TYPE s3,
            KEY_ID '{_quote_secret_value(credentials.access_key_id)}',
            SECRET '{_quote_secret_value(credentials.secret_access_key)}',
            SESSION_TOKEN '{_quote_secret_value(credentials.session_token)}',
            REGION '{_FILES_S3_REGION}',
            ENDPOINT '{_quote_secret_value(endpoint)}',
            URL_STYLE 'path',
            USE_SSL {use_ssl}
        )
        """
    )


def _jsonable(value: object) -> object:
    """Ép mọi kiểu Arrow-native mà `json` chuẩn không tự đọc được thành `str`.

    `pa.Table.to_pylist()` trả `datetime.date`/`datetime.datetime` cho cột
    ngày/giờ, `decimal.Decimal` cho cột số thập phân chính xác, và `bytes` cho
    cột nhị phân. FastAPI/Pydantic SẼ mã hoá được một phần trong số đó qua
    `jsonable_encoder` khi trường đích khai `Any`, nhưng dựa vào đoán đó cho
    một trường `list[list[Any]]` là một giả định KHÔNG kiểm được ở tầng kiểu.
    Ép tường minh ở đây thì định dạng ổn định, không phụ thuộc phiên bản
    Pydantic đang cài.
    """
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if type(value).__name__ == "Decimal":
        return str(value)
    return value


def _to_result(table: pa.Table, *, truncated: bool, row_count: int) -> QueryResult:
    columns = [ColumnOut(name=f.name, type=str(f.type)) for f in table.schema]
    rows = [[_jsonable(row[name]) for name in table.column_names] for row in table.to_pylist()]
    return QueryResult(columns=columns, rows=rows, truncated=truncated, row_count=row_count)


def _real_qualified_name(ref: TableRef) -> str:
    """`namespace.table` THẬT bên trong lakehouse sở hữu `ref` — dùng để gọi
    `Lakehouse.scan()`/`scan_size_bytes()` trên catalog Iceberg của chính
    lakehouse đó.

    Với bảng ba phần, `ref.namespace` mang tiền tố alias lakehouse
    (`"kho_khac.finance"`) — catalog Iceberg của `kho_khac` không biết gì về
    cái tên `kho_khac` (đó là tên item trong control plane, không phải một
    phần của namespace Iceberg thật), nên phải `strip_lakehouse_alias` trước
    khi hỏi nó.
    """
    assert ref.namespace is not None  # `run_gate` đã từ chối namespace=None
    return f"{strip_lakehouse_alias(ref.namespace)}.{ref.name}"


def _view_target(
    connection: duckdb.DuckDBPyConnection,
    ref: TableRef,
    *,
    attached_catalogs: set[str],
    schemas_created: dict[str, set[str]],
) -> str:
    """Tạo (nếu chưa có) schema/catalog DuckDB cần thiết cho `ref`, trả về tên
    view đủ điều kiện — ĐÚNG hình dạng SQL người dùng đã gõ, để câu SQL chạy
    KHÔNG sửa đổi.

    **Bảng HAI phần** (`namespace.table`): dùng catalog MẶC ĐỊNH của
    connection (`memory`, do `duckdb.connect(":memory:")` tạo) — SQL người
    dùng viết `namespace.table`, không mang tiền tố catalog nào, nên DuckDB tự
    tìm trong catalog hiện hành.

    **Bảng BA phần** (`lakehouse.namespace.table`): đã kiểm bằng thực nghiệm
    (duckdb 1.5.5) rằng ba phần cách nhau bởi dấu chấm, KHÔNG đặt trong ngoặc
    kép, LUÔN được DuckDB đọc là `catalog.schema.table` — không có cách nào
    khác để một chuỗi "ba định danh cách dấu chấm" khớp một schema/tên mang
    dấu chấm bên trong (đã thử: tạo schema tên `"finance.reports"`/view tên
    ghép gạch dưới rồi hỏi bằng cú pháp ba phần — đều ném `Binder Error:
    Catalog "..." does not exist`). Nên phải `ATTACH ':memory:' AS <alias>`
    một catalog DuckDB THẬT mang đúng tên `alias` (bí danh người dùng gõ trong
    SQL, KHÔNG phải id lakehouse), rồi tạo schema/view bên trong catalog đó.

    Hai lakehouse khác nhau LUÔN được `ATTACH` dưới hai alias khác nhau (tên
    lakehouse là duy nhất trong một workspace — xem `uq_item_active_name`),
    nên `namespace.table` trùng tên giữa hai lakehouse KHÔNG đè lên nhau: mỗi
    bên nằm trong catalog DuckDB riêng của nó, đã kiểm bằng thực nghiệm (hai
    catalog `ATTACH` cùng có schema/view `finance.reports`, đọc độc lập, không
    lệch dữ liệu).
    """
    assert ref.namespace is not None
    alias = lakehouse_alias_of(ref.namespace)

    if alias is None:
        schema = ref.namespace
        if schema not in schemas_created[""]:
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema)}")
            schemas_created[""].add(schema)
        return f"{_quote_ident(schema)}.{_quote_ident(ref.name)}"

    if alias not in attached_catalogs:
        connection.execute(f"ATTACH ':memory:' AS {_quote_ident(alias)}")
        attached_catalogs.add(alias)
    schema = strip_lakehouse_alias(ref.namespace)
    if schema not in schemas_created[alias]:
        connection.execute(
            f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(alias)}.{_quote_ident(schema)}"
        )
        schemas_created[alias].add(schema)
    return f"{_quote_ident(alias)}.{_quote_ident(schema)}.{_quote_ident(ref.name)}"


def _run_sync(
    *,
    sql: str,
    resolved_tables: tuple[ResolvedTable, ...],
    settings: Settings,
    publish_connection: Callable[[duckdb.DuckDBPyConnection], None],
    workspace_id: uuid.UUID,
    lakehouse_id: uuid.UUID,
    storage: StorageCredentials,
) -> QueryResult:
    """Phần CHẶN: mở connection, mở MỘT catalog Iceberg cho mỗi lakehouse chạm
    tới, đăng ký từng bảng trên đúng catalog của nó, chạy `sql`.

    `workspace_id`/`lakehouse_id`/`storage` phục vụ ĐƯỜNG KHÁC, ĐỘC LẬP với
    `resolved_tables` — đọc `Files/` thô qua `read_parquet`/`read_csv` (Task
    13), không đi qua Iceberg/Lakekeeper chút nào (xem module docstring
    `loom_query.files`). Cần truyền RIÊNG (không suy ra từ `resolved_tables`)
    vì một câu SQL CHỈ đọc `Files/` có `resolved_tables == ()` — không có
    bảng catalog nào để lấy `lakehouse_id` ra từ đó.

    `resolved_tables` (trả về từ `authz.run_gate`) đã gắn sẵn id lakehouse sở
    hữu MỖI bảng — bảng hai phần mang id của chính request, bảng ba phần mang
    id đã phân giải từ tên lakehouse khác. `_lakehouse_for` bên dưới dựng MỘT
    `Lakehouse` (catalog Iceberg) cho mỗi id KHÁC NHAU, dùng lại khi hai bảng
    cùng lakehouse — không dựng lại cho từng bảng, và không dồn hai lakehouse
    khác nhau vào chung một catalog (xem module docstring cho lý do cả hai vế
    đó đều bắt buộc).

    Mỗi lời gọi `build_catalog` dựng một `RestCatalog` MỚI — đúng khuyến nghị
    của `loom_iceberg.catalog`: mỗi query một catalog riêng của chính nó,
    không chia sẻ giữa các query chạy đồng thời.

    DuckDB không hiểu `namespace.table` trỏ thẳng vào một object Python đã
    đăng ký (`register()` luôn đặt object vào schema `main` của catalog HIỆN
    HÀNH, có thử nghiệm ở Task 6) — nên với mỗi bảng: đăng ký
    `RecordBatchReader` dưới một tên KHÔNG dấu chấm, rồi tạo một `VIEW` mang
    đúng tên bảng NHƯ SQL VIẾT (`_view_target`) chiếu qua object đã đăng ký.
    Đã kiểm bằng thực nghiệm rằng một `CREATE VIEW` trong một catalog ATTACH
    vẫn `SELECT` được từ một object `register()` ở catalog mặc định — không
    cần `USE` catalog nào cả.

    `connection` được `publish_connection` NGAY sau khi tạo — TRƯỚC cả
    `build_catalog` — để `execute()` (chạy trên event loop) có cơ chế
    `interrupt()` sớm nhất có thể, và để một lỗi xảy ra SAU đó (catalog sập,
    bảng không tồn tại, byte quét vượt trần...) không bao giờ làm `execute()`
    chờ vô hạn một connection không tới (xem docstring `execute`).

    Giới hạn 2 (byte quét, Task 8) kiểm NGAY SAU khi có mọi `Lakehouse` cần
    dùng, TRƯỚC vòng lặp đăng ký bảng bên dưới — tức là trước MỌI lời gọi
    `lakehouse.scan()` (đọc thật), của BẤT KỲ lakehouse nào. Đảo thứ tự hai
    khối này là đúng lỗi "chứng minh đỏ 1" của Task 8 chặn: phép kiểm phải
    thấy `Lakehouse.scan` chưa từng được gọi khi bị từ chối, xem
    `tests/integration/test_query_scan_bytes.py`.
    """
    connection = duckdb.connect(":memory:")
    publish_connection(connection)
    try:
        connection.execute(f"SET memory_limit='{_MEMORY_LIMIT}'")
        connection.execute(f"SET threads={_THREADS}")

        # Đường Files/ (Task 13) — ĐỘC LẬP với đường Iceberg bên dưới, không
        # chạm `resolved_tables`/`lakehouses` gì cả. `resolve_files_query` AN
        # TOÀN gọi trên MỌI câu SQL: nó tự trả nguyên `sql` không đổi nếu
        # không có `read_parquet`/`read_csv` nào (xem docstring của nó), nên
        # gọi VÔ ĐIỀU KIỆN ở đây rẻ hơn nhiều so với một nhánh rẽ dựa vào một
        # cờ TÍNH SẴN lúc `run_gate` — cờ đó sẽ là một nguồn sự thật THỨ HAI
        # về "câu SQL này có đọc Files/ hay không", đúng cái bẫy mà
        # `authz.run_gate`/`_resolve_tables` đã né cho `lakehouse_id` (xem
        # comment ở đó). `validate_files_paths` (bên trong) đã chạy một lần ở
        # `run_gate` TRƯỚC khi có `202` — gọi lại ở đây là một lớp phòng hờ
        # RẺ (thuần AST, không I/O), không phải một round trip STS lãng phí:
        # round trip đó (`_install_files_secret`) CHỈ chạy nếu `has_file_reads`.
        files_query = resolve_files_query(
            sql,
            DIALECT,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            bucket=settings.storage_bucket,
        )
        if files_query.has_file_reads:
            _install_files_secret(
                connection, storage=storage, workspace_id=workspace_id, settings=settings
            )
        sql_to_run = files_query.sql

        lakehouses: dict[uuid.UUID, Lakehouse] = {}

        def lakehouse_for(lakehouse_id: uuid.UUID) -> Lakehouse:
            cached = lakehouses.get(lakehouse_id)
            if cached is None:
                catalog = build_catalog(
                    catalog_uri=settings.catalog_uri,
                    warehouse=str(lakehouse_id),
                    s3_endpoint=settings.s3_endpoint,
                )
                cached = Lakehouse(catalog)
                lakehouses[lakehouse_id] = cached
            return cached

        scan_targets = [
            (lakehouse_for(table.lakehouse_id), _real_qualified_name(table.ref))
            for table in resolved_tables
        ]
        check_scan_bytes(scan_targets, settings.max_scan_bytes)

        attached_catalogs: set[str] = set()
        schemas_created: dict[str, set[str]] = defaultdict(set)

        for index, table in enumerate(resolved_tables):
            ref = table.ref
            lakehouse = lakehouse_for(table.lakehouse_id)
            reader = lakehouse.scan(_real_qualified_name(ref))

            raw_name = f"__loom_raw_{index}"
            connection.register(raw_name, reader)

            qualified_view = _view_target(
                connection,
                ref,
                attached_catalogs=attached_catalogs,
                schemas_created=schemas_created,
            )
            # S608 (SQL injection qua f-string) là báo động giả ở đây:
            # `qualified_view` chỉ chứa định danh đã qua `_quote_ident` (trích
            # dẫn + thoát dấu `"`), và `raw_name` do CHÍNH hàm này sinh từ
            # `index`, không phải input người dùng — không đoạn nào trong
            # chuỗi dưới đây mang một chuỗi SQL do người gọi tự do gõ vào.
            connection.execute(f"CREATE VIEW {qualified_view} AS SELECT * FROM {raw_name}")  # noqa: S608

        # `.arrow()` trả `pa.RecordBatchReader` (đã kiểm bằng thực nghiệm trên
        # duckdb 1.5.5 — KHÁC tài liệu cũ hơn từng nói nó trả `pa.Table`).
        # `.to_arrow_table()` là API thay thế cho `.fetch_arrow_table()` (bản
        # cũ đã bị deprecate) và trả đúng `pa.Table` mà `_to_result` cần —
        # bảng kết quả cuối (đã bị trần 10.000 dòng của giới hạn 3) không cần
        # đọc theo luồng như `Lakehouse.scan()` ở trên.
        arrow_result = connection.sql(sql_to_run).to_arrow_table()
        limited, truncated, row_count = truncate_table(arrow_result, settings.max_result_rows)
        return _to_result(limited, truncated=truncated, row_count=row_count)
    finally:
        connection.close()


async def execute(
    *,
    query_id: uuid.UUID,
    sql: str,
    resolved_tables: tuple[ResolvedTable, ...],
    settings: Settings,
    store: QueryStore,
    workspace_id: uuid.UUID,
    lakehouse_id: uuid.UUID,
    storage: StorageCredentials,
) -> None:
    """Task nền của một query — ghi kết quả (hoặc lỗi) vào `store` khi xong.

    Bảng catalog KHÔNG nhận `lakehouse_id` riêng: mỗi phần tử của
    `resolved_tables` đã tự mang id lakehouse sở hữu nó (kể cả bảng hai phần,
    mang id của chính request — xem `authz._resolve_tables`), và `_run_sync`
    mở catalog theo ĐÚNG những id đó.

    **`workspace_id`/`lakehouse_id`/`storage` ở đây LÀ một trường hợp khác —
    và KHÔNG mâu thuẫn với đoạn trên:** chúng phục vụ đường Files/ (Task 13),
    ĐỘC LẬP với `resolved_tables`, xem docstring `_run_sync`. Một câu SQL CHỈ
    đọc `Files/` có `resolved_tables == ()`, nên `lakehouse_id`/`workspace_id`
    của REQUEST (không phải của một bảng catalog nào) là nguồn duy nhất để
    biết "Files/ của lakehouse nào" — không có cách nào suy ra nó từ
    `resolved_tables` khi danh sách đó rỗng.

    Bắt MỌI ngoại lệ: đây là ranh giới của một `asyncio.Task` không ai `await`
    trực tiếp (`routers/query.py` chỉ giữ tham chiếu để huỷ, không đợi nó),
    nên một ngoại lệ không bắt ở đây biến mất vào log "Task exception was
    never retrieved" thay vì trở thành một trạng thái `failed` mà client
    `GET` được — với người đang chờ kết quả thì hai thứ đó khác nhau hoàn
    toàn: một cái nói được lý do, một cái treo mãi ở "running".

    **Giới hạn 1 (thời gian, Task 8) và huỷ thật (Task 9) dùng CHUNG một cơ
    chế.** `_run_sync` chạy trong một thread riêng (`asyncio.to_thread`); ngay
    khi nó tạo connection, nó gọi `publish_connection` (bên dưới) để đưa
    connection đó về LẠI event loop qua `loop.call_soon_threadsafe` — đây là
    đường DUY NHẤT an toàn để một thread khác chạm vào một `asyncio.Future`
    (không được gọi `set_result` trực tiếp từ ngoài event loop). Từ đó,
    `store.attach_interrupt` gắn `connection.interrupt` làm cơ chế huỷ (Task
    9 dùng khi `DELETE` tới), và nếu quá `settings.query_timeout_seconds`
    giây mà `run_task` chưa xong, nhánh timeout bên dưới gọi CHÍNH cơ chế đó
    rồi mới báo lỗi — tức là timeout cũng THẬT SỰ dừng việc, không chỉ báo lỗi
    rồi bỏ mặc thread nền chạy tiếp.

    `asyncio.shield(run_task)`: khi `wait_for` hết giờ, nó chỉ huỷ CÁI SHIELD
    (từ bỏ việc chờ), không huỷ `run_task` — `run_task` (và thread bên dưới nó)
    tiếp tục sống để nhánh `except TimeoutError` tự tay `interrupt()` rồi
    `await run_task` lấy nốt kết quả (bỏ đi) của nó, tránh cảnh báo "Task
    exception was never retrieved" từ MỘT future khác (future bọc thread, không
    phải `run_task`).
    """
    loop = asyncio.get_running_loop()
    connection_future: asyncio.Future[duckdb.DuckDBPyConnection] = loop.create_future()

    def publish_connection(connection: duckdb.DuckDBPyConnection) -> None:
        # Gọi TỪ thread nền — không được chạm `store`/`connection_future`
        # trực tiếp ở đây (chỉ event loop mới an toàn sửa chúng, xem docstring
        # `store.py`: "một event loop DUY NHẤT"). `call_soon_threadsafe` marshal
        # đúng việc `set_result` về event loop.
        loop.call_soon_threadsafe(connection_future.set_result, connection)

    run_task = asyncio.create_task(
        asyncio.to_thread(
            _run_sync,
            sql=sql,
            resolved_tables=resolved_tables,
            settings=settings,
            publish_connection=publish_connection,
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
            storage=storage,
        )
    )

    connection = await connection_future
    await store.attach_interrupt(query_id, connection.interrupt)

    try:
        result = await asyncio.wait_for(
            asyncio.shield(run_task), timeout=settings.query_timeout_seconds
        )
    except TimeoutError:
        connection.interrupt()
        with contextlib.suppress(Exception):
            await run_task
        await store.set_failed(
            query_id,
            f"query exceeded the {settings.query_timeout_seconds:g}s time limit and was stopped",
        )
        return
    except Exception as exc:  # mọi lỗi đều thành "failed" (BLE001 không nằm trong ruleset dự án)
        await store.set_failed(query_id, str(exc))
        return

    await store.set_succeeded(
        query_id,
        result.columns,
        result.rows,
        truncated=result.truncated,
        row_count=result.row_count,
    )
