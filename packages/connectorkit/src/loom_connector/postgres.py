"""`PostgresConnector` — cài đặt THẬT của `Connector`, đọc trực tiếp từ Postgres.

Hai quyết định trong file này tồn tại vì đúng một lý do: Đo 1 của Giai đoạn 3a
(xem `Makefile`, target `measure-ingest-pod`) đo pod ingest ở 406-421 MiB đỉnh
CHỈ với việc ghi Iceberg — RAM còn lại cho việc ĐỌC nguồn gần như bằng không.
Cursor bình thường của psycopg kéo TOÀN BỘ kết quả về client trong lúc
`execute()` chạy, trước khi dòng đầu tiên được trả về caller. Trên một bảng
vài GB, đó là OOMKill, và nó nổ ra ở bảng lớn ĐẦU TIÊN gặp phải — xa hẳn chỗ
gây ra nó (chỗ gây ra là dòng `conn.cursor()` thiếu `name=`).

**Cursor CÓ TÊN (`name=...`) = server-side.** Nó làm Postgres giữ tập kết quả
ở phía server và gửi về theo từng đợt `FETCH FORWARD`, thay vì đẩy hết một
lần. Đã đo thật (script rời, không giữ trong repo, cùng bảng ~50 MB payload,
lấy đúng một lô 100 dòng): cursor có tên tăng RSS tiến trình +0.4 MiB; cursor
thường (bỏ `name=`) tăng +98.8 MiB cho CÙNG một lô — chênh ~250 lần, chứng
minh cursor thường đã kéo hết bảng về ngay từ `execute()` bất kể sau đó gọi
`fetchmany()` hay `fetchall()`.

**`itersize` khớp `batch_rows`.** Hai con số điều khiển hai thứ khác nhau: số
dòng psycopg lấy MỖI VÒNG MẠNG (itersize) và số dòng trong MỘT `RecordBatch`
(batch_rows). Lệch nhau thì "một batch" được ghép từ nhiều vòng mạng (hoặc
một vòng mạng bị cắt thành nhiều batch nhỏ), và RAM đỉnh phụ thuộc bội số của
cả hai chứ không phải một con số duy nhất dễ suy luận. Đặt bằng nhau thì "một
batch" chỉ còn đúng MỘT nghĩa.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pyarrow as pa  # type: ignore[import-untyped]
from psycopg import sql
from psycopg.rows import dict_row

from loom_connector.protocol import (
    CheckResult,
    ColumnSchema,
    CursorCandidate,
    StreamSchema,
    StreamState,
)
from loom_core.cursor import CURSOR_TYPE_ALLOWLIST

# Kiểu Postgres DÙNG ĐƯỢC làm watermark. Danh sách và LÝ DO loại TEXT/NUMERIC
# nằm ở `loom_core.cursor` — import ngược lên chứ KHÔNG giữ một bản chép ở đây:
# `loom-api` kiểm cùng danh sách đó khi pod nạp báo `cursor_type` về
# (`routers/internal_ingest.py`), và hai bản chép trôi khỏi nhau nghĩa là API
# từ chối đúng cái cursor mà `discover()` ngay dưới đây vừa đề xuất. Đó là chỗ
# DUY NHẤT `loom_connector` được phép chạm `loom_core` — xem allowlist trong
# `tests/test_connector_no_io.py`.

_ARROW_TYPE_MAP: dict[str, pa.DataType] = {
    "smallint": pa.int16(),
    "integer": pa.int32(),
    "bigint": pa.int64(),
    "real": pa.float32(),
    "double precision": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "timestamp without time zone": pa.timestamp("us"),
    "timestamp with time zone": pa.timestamp("us", tz="UTC"),
    "text": pa.string(),
    "character varying": pa.string(),
    "character": pa.string(),
    "uuid": pa.string(),
    "json": pa.string(),
    "jsonb": pa.string(),
    # NUMERIC -> string, KHÔNG float64: NUMERIC của Postgres là thập phân
    # CHÍNH XÁC (arbitrary precision), còn float64 là nhị phân xấp xỉ — ép về
    # float64 làm mất chính xác ÂM THẦM (không lỗi, không cảnh báo), và trên
    # một cột tiền tệ đó là lỗi NGHIỆP VỤ chứ không phải sai số làm tròn vô
    # hại. Bronze giữ nguyên chuỗi thập phân nguồn; silver ép về kiểu decimal
    # thật khi đã biết precision/scale nó thực sự cần.
    "numeric": pa.string(),
}


def _arrow_type_for(pg_type: str) -> pa.DataType:
    # Kiểu lạ (mảng, enum, kiểu người dùng tự định nghĩa, ...) rơi về string
    # thay vì ném lỗi: một connector từ chối cả bảng vì MỘT cột lạ là tệ hơn
    # một cột chưa được ép kiểu tối ưu — string luôn nhận được mọi giá trị
    # Postgres trả về dưới dạng text an toàn.
    return _ARROW_TYPE_MAP.get(pg_type, pa.string())


def _parse_stream(stream: str) -> tuple[str, str]:
    parts = stream.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"stream phải có dạng 'schema.table', nhận {stream!r}")
    return parts[0], parts[1]


def _coerce_for_arrow(value: object) -> object:
    """psycopg trả NUMERIC dưới dạng `decimal.Decimal` — `pa.array` với
    `type=pa.string()` không tự chuyển được `Decimal` (ném `ArrowInvalid`), nên
    phải ép về `str` tại đây. Mọi kiểu khác (int, bool, `datetime`, `date`,
    `str`, `None`) psycopg đã trả sẵn đúng dạng mà pyarrow nhận trực tiếp cho
    kiểu Arrow tương ứng trong `_ARROW_TYPE_MAP`."""
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


@dataclass(frozen=True, slots=True)
class _ColumnInfo:
    name: str
    arrow_type: pa.DataType
    nullable: bool


def _rows_to_record_batch(
    rows: list[dict[str, object]], columns: tuple[_ColumnInfo, ...]
) -> pa.RecordBatch:
    fields = [pa.field(c.name, c.arrow_type, nullable=c.nullable) for c in columns]
    arrays = [
        pa.array([_coerce_for_arrow(row[c.name]) for row in rows], type=c.arrow_type)
        for c in columns
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))


class PostgresConnector:
    """`schema`: giới hạn `discover()` vào MỘT namespace Postgres thay vì quét
    toàn bộ database. Có ích thật ở production (một Postgres nguồn thường có
    nhiều schema — theo tenant, theo ứng dụng — và người cấu hình ingest chỉ
    muốn thấy bảng của MÌNH), và cũng là cách hai bộ test của Task 5 (bộ hợp
    đồng dùng chung `source_dsn` với bộ test riêng của connector này) đứng
    trên CÙNG MỘT container Postgres mà không giẫm lên bảng của nhau: mỗi bộ
    tạo bảng trong schema riêng, rồi giới hạn `discover()` vào đúng schema đó.
    """

    def __init__(self, dsn: str, batch_rows: int = 10_000, schema: str | None = None) -> None:
        if batch_rows <= 0:
            raise ValueError("batch_rows phải dương")
        self._dsn = dsn
        self._batch_rows = batch_rows
        self._schema = schema

    def check(self) -> CheckResult:
        """TRẢ VỀ thất bại, không bao giờ ném — đây là điều UI gọi sau nút "Test
        connection" (spec Giai đoạn 3c), và một stack trace không phải một
        thông báo người vận hành đọc được. Bắt `Exception` rộng có chủ đích:
        DNS không phân giải được, connection refused, sai mật khẩu, sai tên
        database... đều phải biến thành CÙNG một `CheckResult(ok=False)`,
        không phải một danh sách ngoại lệ phải đoán trước.

        `connect_timeout=10`: không đặt thì một host bị lọc gói tin (thay vì
        từ chối thẳng) khiến `connect()` treo tới timeout TCP mặc định của hệ
        điều hành — trên Linux thường trên một phút, quá lâu cho một nút bấm
        UI đang chờ phản hồi.
        """
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn, conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
        except Exception as exc:  # bắt rộng có chủ đích — xem docstring phía trên
            return CheckResult(ok=False, message=f"không kết nối được Postgres: {exc}")
        version = row[0] if row else "?"
        return CheckResult(ok=True, message=f"kết nối Postgres thành công — {version}")

    def discover(self) -> list[StreamSchema]:
        if self._schema is not None:
            where = sql.SQL("table_schema = %s")
            params: tuple[object, ...] = (self._schema,)
        else:
            # information_schema.columns liệt kê CẢ cột của các bảng hệ thống
            # (pg_catalog, information_schema) — loại chúng ra, không ai muốn
            # nạp catalog nội bộ của chính Postgres như một "stream".
            where = sql.SQL("table_schema NOT IN ('pg_catalog', 'information_schema')")
            params = ()
        query = sql.SQL(
            "SELECT table_schema, table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE {} "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).format(where)

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        columns_by_stream: dict[tuple[str, str], list[tuple[str, str, bool]]] = {}
        for schema_name, table_name, column_name, pg_type, is_nullable in rows:
            key = (schema_name, table_name)
            columns_by_stream.setdefault(key, []).append(
                (column_name, pg_type, is_nullable == "YES")
            )

        streams = []
        for (schema_name, table_name), cols in columns_by_stream.items():
            columns = tuple(
                ColumnSchema(name=name, arrow_type=_arrow_type_for(pg_type), nullable=nullable)
                for name, pg_type, nullable in cols
            )
            # Kiểu đi CÙNG tên, không bị ném đi: `pg_type` ở đây chính là chuỗi
            # mà `loom-api` sẽ kiểm lại khi pod nạp báo `cursor_type` về, và nó
            # đã nằm trong tay ở đúng dòng này. Xem `CursorCandidate` cho lý do
            # suy ngược từ `arrow_type` không phải một phương án.
            candidate_cursors = tuple(
                CursorCandidate(name=name, cursor_type=pg_type)
                for name, pg_type, _ in cols
                if pg_type in CURSOR_TYPE_ALLOWLIST
            )
            streams.append(
                StreamSchema(
                    name=f"{schema_name}.{table_name}",
                    columns=columns,
                    candidate_cursors=candidate_cursors,
                )
            )
        return streams

    def read(self, stream: str, state: StreamState) -> Iterator[pa.RecordBatch]:
        """Validate NGAY khi gọi (tên dạng `schema.table`, bảng thật sự tồn
        tại) rồi mới trả về một generator — cùng lý do `FakeConnector` tách
        `read()` khỏi `_read()` (xem docstring của nó): nếu việc kiểm tra nằm
        trong thân generator, `read("stream-la", state)` sẽ trả về một
        iterator "hợp lệ" và chỉ nổ ở lần `next()` đầu tiên, không phải ngay
        lúc gọi — quá muộn để phân biệt "cấu hình sai" với "đọc hỏng giữa
        chừng"."""
        schema_name, table_name = _parse_stream(stream)
        conn = psycopg.connect(self._dsn)
        try:
            columns = self._columns_for(conn, schema_name, table_name)
        except Exception:
            conn.close()
            raise
        return self._read_rows(conn, schema_name, table_name, columns, state)

    def _columns_for(
        self, conn: psycopg.Connection, schema_name: str, table_name: str
    ) -> tuple[_ColumnInfo, ...]:
        query = sql.SQL(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
        )
        with conn.cursor() as cur:
            cur.execute(query, (schema_name, table_name))
            rows = cur.fetchall()
        if not rows:
            raise ValueError(f"stream '{schema_name}.{table_name}' không tồn tại")
        return tuple(
            _ColumnInfo(
                name=name, arrow_type=_arrow_type_for(pg_type), nullable=(is_nullable == "YES")
            )
            for name, pg_type, is_nullable in rows
        )

    def _read_rows(
        self,
        conn: psycopg.Connection,
        schema_name: str,
        table_name: str,
        columns: tuple[_ColumnInfo, ...],
        state: StreamState,
    ) -> Iterator[pa.RecordBatch]:
        try:
            select_list = sql.SQL(", ").join(sql.Identifier(c.name) for c in columns)
            # Tên schema/bảng/cột đi qua `sql.Identifier`, KHÔNG BAO GIỜ nội
            # suy bằng f-string: chúng tới từ cấu hình do người dùng nhập ở
            # UI (spec Giai đoạn 3c), và một dấu nháy kép trong tên bảng không
            # được phép trở thành SQL injection.
            query = sql.SQL("SELECT {} FROM {}.{}").format(
                select_list, sql.Identifier(schema_name), sql.Identifier(table_name)
            )
            params: tuple[object, ...] = ()
            if state.cursor_column is not None:
                if state.cursor_value is not None:
                    # `>=`, KHÔNG `>`. Dòng có giá trị cursor ĐÚNG BẰNG mốc
                    # watermark bắt buộc phải xuất hiện lại: `>` mất dữ liệu
                    # khi nhiều dòng chia sẻ cùng giá trị cursor commit lệch
                    # thời điểm, còn `>=` chỉ sinh trùng lặp — trùng đếm được
                    # và khử được ở silver, mất thì không để lại dấu vết nào.
                    query = sql.SQL("{} WHERE {} >= %s").format(
                        query, sql.Identifier(state.cursor_column)
                    )
                    params = (state.cursor_value,)
                # ORDER BY áp dụng bất kể có `cursor_value` hay chưa: một lần
                # đọc đầy đủ đầu tiên (cursor_column đã chọn, cursor_value
                # chưa có) vẫn cần thứ tự xác định để lần đọc gia tăng SAU
                # tiếp tục đúng chỗ nó dừng, và để phép kiểm round-trip kiểu
                # Arrow đọc được một hàng cụ thể một cách xác định.
                query = sql.SQL("{} ORDER BY {}").format(query, sql.Identifier(state.cursor_column))

            with conn.cursor(name="loom_connector_read", row_factory=dict_row) as cur:
                # itersize == batch_rows: xem docstring đầu file — một lô
                # Arrow ứng với ĐÚNG MỘT vòng FETCH phía server.
                cur.itersize = self._batch_rows
                cur.execute(query, params)
                batch: list[dict[str, object]] = []
                for row in cur:
                    batch.append(row)
                    if len(batch) >= self._batch_rows:
                        yield _rows_to_record_batch(batch, columns)
                        batch = []
                if batch:
                    yield _rows_to_record_batch(batch, columns)
        finally:
            conn.close()
