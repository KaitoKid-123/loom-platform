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
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
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
from loom_query.config import Settings
from loom_query.limits import check_scan_bytes, truncate_table
from loom_query.schemas import ColumnOut
from loom_query.store import QueryStore
from loom_sql import TableRef

_MEMORY_LIMIT = "256MB"
_THREADS = 2


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


def _run_sync(
    *,
    sql: str,
    lakehouse_id: uuid.UUID,
    table_refs: tuple[TableRef, ...],
    settings: Settings,
    publish_connection: Callable[[duckdb.DuckDBPyConnection], None],
) -> QueryResult:
    """Phần CHẶN: mở connection, mở catalog, đăng ký từng bảng, chạy `sql`.

    Warehouse của Lakekeeper đặt tên theo `str(lakehouse_id)` — quy ước tạm của
    Giai đoạn 2b (xem `config.py`); Task vòng đời warehouse (bước tạo warehouse
    khi tạo item `lakehouse`) là việc của task sau, chưa dựng ở đây.

    Mỗi lời gọi dựng một `RestCatalog` MỚI qua `build_catalog` — đúng khuyến
    nghị của `loom_iceberg.catalog`: mỗi query một catalog riêng của chính nó,
    không chia sẻ giữa các query chạy đồng thời.

    DuckDB không hiểu `namespace.table` trỏ thẳng vào một object Python đã
    đăng ký (`register()` luôn đặt object vào schema `main`, có thử nghiệm ở
    Task này) — nên với mỗi bảng: đăng ký `RecordBatchReader` dưới một tên
    KHÔNG dấu chấm, tạo `SCHEMA` mang tên namespace nếu chưa có, rồi tạo một
    `VIEW` mang đúng tên `namespace.table` chiếu qua object đã đăng ký. SQL của
    người dùng vì vậy chạy KHÔNG sửa đổi, tham chiếu `namespace.table` y hệt
    những gì họ gõ.

    `connection` được `publish_connection` NGAY sau khi tạo — TRƯỚC cả
    `build_catalog` — để `execute()` (chạy trên event loop) có cơ chế
    `interrupt()` sớm nhất có thể, và để một lỗi xảy ra SAU đó (catalog sập,
    bảng không tồn tại, byte quét vượt trần...) không bao giờ làm `execute()`
    chờ vô hạn một connection không tới (xem docstring `execute`).

    Giới hạn 2 (byte quét, Task 8) kiểm NGAY SAU khi có `lakehouse`, TRƯỚC
    vòng lặp đăng ký bảng bên dưới — tức là trước MỌI lời gọi `lakehouse.scan()`
    (đọc thật). Đảo thứ tự hai khối này là đúng lỗi "chứng minh đỏ 1" của Task
    8 chặn: phép kiểm phải thấy `Lakehouse.scan` chưa từng được gọi khi bị từ
    chối, xem `tests/integration/test_query_scan_bytes.py`.
    """
    connection = duckdb.connect(":memory:")
    publish_connection(connection)
    try:
        connection.execute(f"SET memory_limit='{_MEMORY_LIMIT}'")
        connection.execute(f"SET threads={_THREADS}")

        catalog = build_catalog(
            catalog_uri=settings.catalog_uri,
            warehouse=str(lakehouse_id),
            s3_endpoint=settings.s3_endpoint,
        )
        lakehouse = Lakehouse(catalog)

        check_scan_bytes(lakehouse, table_refs, settings.max_scan_bytes)

        namespaces_created: set[str] = set()
        for index, ref in enumerate(table_refs):
            # `run_gate` đã từ chối mọi `TableRef` có `namespace is None` bằng
            # 400 trước khi `run_sync` từng được gọi — xem `_resolve_item_id`.
            assert ref.namespace is not None
            reader = lakehouse.scan(f"{ref.namespace}.{ref.name}")

            raw_name = f"__loom_raw_{index}"
            connection.register(raw_name, reader)

            if ref.namespace not in namespaces_created:
                connection.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(ref.namespace)}")
                namespaces_created.add(ref.namespace)

            # S608 (SQL injection qua f-string) là báo động giả ở đây: cả hai
            # định danh đi qua `_quote_ident` (trích dẫn + thoát dấu `"`), và
            # `raw_name` do CHÍNH hàm này sinh từ `index`, không phải input
            # người dùng — không đoạn nào trong chuỗi dưới đây mang một chuỗi
            # SQL do người gọi tự do gõ vào.
            qualified_view = f"{_quote_ident(ref.namespace)}.{_quote_ident(ref.name)}"
            connection.execute(f"CREATE VIEW {qualified_view} AS SELECT * FROM {raw_name}")  # noqa: S608

        # `.arrow()` trả `pa.RecordBatchReader` (đã kiểm bằng thực nghiệm trên
        # duckdb 1.5.5 — KHÁC tài liệu cũ hơn từng nói nó trả `pa.Table`).
        # `.to_arrow_table()` là API thay thế cho `.fetch_arrow_table()` (bản
        # cũ đã bị deprecate) và trả đúng `pa.Table` mà `_to_result` cần —
        # bảng kết quả cuối (đã bị trần 10.000 dòng của giới hạn 3) không cần
        # đọc theo luồng như `Lakehouse.scan()` ở trên.
        arrow_result = connection.sql(sql).to_arrow_table()
        limited, truncated, row_count = truncate_table(arrow_result, settings.max_result_rows)
        return _to_result(limited, truncated=truncated, row_count=row_count)
    finally:
        connection.close()


async def execute(
    *,
    query_id: uuid.UUID,
    sql: str,
    lakehouse_id: uuid.UUID,
    table_refs: tuple[TableRef, ...],
    settings: Settings,
    store: QueryStore,
) -> None:
    """Task nền của một query — ghi kết quả (hoặc lỗi) vào `store` khi xong.

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
            lakehouse_id=lakehouse_id,
            table_refs=table_refs,
            settings=settings,
            publish_connection=publish_connection,
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
