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

**Kiểu nguồn về Python dạng gì: ĐO, không nhớ.** Bản trước ánh xạ `uuid`,
`json`, `jsonb` sang `pa.string()` rồi để psycopg trả `uuid.UUID` và `dict`;
pyarrow từ chối cả hai (`ArrowTypeError: Expected bytes, got a 'UUID' object`),
nên MỌI bảng nguồn có một cột uuid hoặc jsonb đều không nạp được — kể cả mọi
bảng trong schema của chính dự án này. Nguyên nhân gốc KHÔNG phải thiếu hai
nhánh ép kiểu: nó là hai chỗ cùng mô tả một hợp đồng ("cột kiểu này về Python
dạng gì") mà không gì bắt chúng khớp nhau — `_ARROW_TYPE_MAP` nói kiểu Arrow,
một hàm `_coerce_for_arrow` riêng nói giá trị nào cần sửa. Hai bảng trôi khỏi
nhau, và chỗ trôi chỉ lộ ra khi gặp đúng cột đó ở production.

Bản này bỏ hẳn bước ép kiểu phía Python: `_needs_text_cast` là chỗ DUY NHẤT
quyết định, và nó để chính Postgres kết xuất giá trị ra text trước khi giá trị
rời server. Số đo của từng kiểu nằm ở `_arrow_type_for` và `_needs_text_cast`;
phép canh giữ chúng không trôi lại là `test_every_mapped_type_round_trips` và
`test_the_type_zoo_covers_every_mapped_type` (chạy trên Postgres 17 thật).
"""

from __future__ import annotations

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

# Khoá là giá trị `information_schema.columns.data_type` ĐÚNG NGUYÊN VĂN (chuỗi
# chuẩn SQL, có dấu cách), không phải tên rút gọn của `pg_catalog` — cùng quy
# ước với `CURSOR_TYPE_ALLOWLIST` ngay trên, và vì cùng một lý do: đó là chuỗi
# `discover()` đọc được.
#
# Những kiểu về kiểu Arrow ĐÚNG NGHĨA (không phải string) là những kiểu psycopg
# đã trả sẵn một giá trị pyarrow nhận trực tiếp. Đã đo trên Postgres 17 +
# psycopg 3.3.4 + pyarrow 25, không suy từ trí nhớ:
#
#     smallint/integer/bigint  -> int                 nhận
#     real/double precision    -> float               nhận
#     boolean                  -> bool                nhận
#     date                     -> datetime.date       nhận
#     timestamp [without tz]   -> datetime naive      nhận
#     timestamp with tz        -> datetime aware      nhận (pyarrow quy về UTC
#                                                     đúng thời điểm kể cả khi
#                                                     server đặt múi giờ khác)
#     text/character varying   -> str                 nhận
#     character                -> str, CÒN đệm đuôi   nhận
#
# Những kiểu về `pa.string()` thì KHÔNG được suy là "psycopg trả str": `uuid`
# trả `uuid.UUID`, `json`/`jsonb` trả `dict`, `numeric` trả `decimal.Decimal`,
# và pyarrow từ chối cả ba trước một mảng string. Chúng đi qua `::text` phía
# Postgres — xem `_needs_text_cast`, nơi giữ toàn bộ lý do.
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
    #
    # "Giữ nguyên chuỗi thập phân nguồn" là một khẳng định về CHUỖI, và nó chỉ
    # đúng nhờ `::text` — `str(decimal.Decimal)` cho ký hiệu khoa học ở số nhỏ,
    # tức là bản trước KHÔNG giữ nguyên. Xem `_needs_text_cast` cho số đo.
    "numeric": pa.string(),
}


def _arrow_type_for(pg_type: str) -> pa.DataType:
    """Kiểu nguồn -> kiểu Arrow. Kiểu LẠ rơi về `pa.string()` thay vì ném lỗi.

    Giữ nguyên tính dễ dãi của bản trước — một connector từ chối cả bảng vì
    MỘT cột lạ tệ hơn một cột chưa được ép kiểu tối ưu — nhưng bỏ LÝ DO mà bản
    trước viết ra cho nó ("string luôn nhận được mọi giá trị Postgres trả về
    dưới dạng text an toàn"). Câu đó không đúng, và nó sai theo hướng nguy
    hiểm nhất: nhánh này tồn tại để DỄ DÃI, mà chính nó biến một cột lạ thành
    sự cố nạp. psycopg dựng SẴN đối tượng Python cho phần lớn kiểu lạ, và
    pyarrow từ chối gần hết chúng trước một mảng string.

    Bảng dưới nói về GIÁ TRỊ PSYCOPG TRẢ THẲNG, chưa qua `::text`; "nhận"
    nghĩa là `pa.array([giá trị], type=pa.string())` chạy được, "từ chối"
    nghĩa là nó ném — và đó là toàn bộ vấn đề. Đo trên Postgres 17 +
    psycopg 3.3.4 + pyarrow 25:

        integer[] / text[]  -> list               từ chối (Expected bytes)
        inet / cidr         -> IPv4Address, ...   từ chối (Expected bytes)
        interval            -> timedelta          từ chối (Expected bytes)
        time [with tz]      -> datetime.time      từ chối (Expected bytes)
        daterange           -> psycopg Range      từ chối (Expected bytes)
        oid                 -> int                từ chối (Expected bytes)
        bytea               -> bytes              từ chối (không phải utf8)
        enum, xml, money, bit, point, macaddr, tsvector, composite -> str  nhận

    Dòng `oid` đáng nhìn kỹ: một `int` bình thường cũng bị từ chối trước một
    mảng string. Nên "kiểu lạ" không phải điều kiện — điều kiện là kiểu Python
    psycopg chọn KHÔNG khớp kiểu Arrow đã khai, và điều đó xảy ra với cả kiểu
    quen thuộc.

    Thứ làm nhánh này dễ dãi THẬT là `_needs_text_cast` ngay dưới: Postgres tự
    kết xuất giá trị ra text TRƯỚC KHI nó rời server, nên cái về tới Python
    luôn là `str` (hoặc `None`), cho mọi kiểu trong bảng trên và cả những kiểu
    chưa ai gặp.
    """
    return _ARROW_TYPE_MAP.get(pg_type, pa.string())


# Kiểu mà psycopg ĐÃ trả `str` VÀ một phép `::text` sẽ LÀM ĐỔI giá trị. Chỉ có
# ba, và lý do nằm ở `character`: phép ép `bpchar -> text` của Postgres CẮT
# khoảng trắng đệm đuôi. Đã đo trên `character(8)` chứa `'abc'` — đọc thẳng ra
# `'abc     '`, đọc qua `::text` ra `'abc'`. Đó đúng là loại biến đổi ÂM THẦM
# mà bronze không được phép làm. `text` và `character varying` thì cast không
# đổi gì (đã đo, kể cả với khoảng trắng đuôi có thật trong dữ liệu), nhưng để
# chúng ở đây cùng `character` giữ cho quy tắc phát biểu được thành MỘT câu:
# cột nào Postgres vốn đã đưa sang dạng text thì không đụng vào.
_NATIVE_TEXT_TYPES = frozenset({"text", "character varying", "character"})


def _needs_text_cast(pg_type: str) -> bool:
    """Cột này có để POSTGRES kết xuất ra text ngay trong câu SELECT không?

    **Đây là chỗ DUY NHẤT quyết định một giá trị về tới Python dưới dạng gì**,
    và đó là cả điểm của nó. Bản trước có HAI chỗ — `_ARROW_TYPE_MAP` nói cột
    về kiểu Arrow nào, `_coerce_for_arrow` nói giá trị Python nào cần sửa
    trước khi giao cho pyarrow — cho cùng MỘT hợp đồng, và không gì bắt chúng
    khớp. Nên `uuid`, `json`, `jsonb` có mặt ở bảng thứ nhất mà vắng ở bảng
    thứ hai, và điều đó chỉ lộ ra ở lần nạp thật đầu tiên gặp một cột uuid.
    Một chỗ quyết định thì không còn hai chỗ để trôi khỏi nhau.

    **Vì sao để Postgres kết xuất chứ không `str()` ở Python.** Chuỗi Postgres
    in ra là chuỗi CỦA NGUỒN — đọc lại vào Postgres cho đúng giá trị cũ. Chuỗi
    `str()` của Python là chuỗi của Python. Chênh lệch không phải giả thuyết,
    đã đo:

        integer[]  Postgres '{1,2,3}'    Python str(list)  '[1, 2, 3]'
        text[]     Postgres '{a,b}'      Python str(list)  "['a', 'b']"
        bytea      Postgres '\\xdeadbeef' Python str(bytes) "b'\\xde\\xad\\xbe\\xef'"
        interval   Postgres '1 day 02:03:04'  Python str(timedelta) '1 day, 2:03:04'
        daterange  Postgres '[2024-01-01,2024-02-01)'
                   Python str(Range) 'Range(datetime.date(2024, 1, 1), ...)'

    **`numeric` là một lỗi CÓ THẬT của bản trước, không phải ví dụ giả định.**
    `str(decimal.Decimal)` chuyển sang ký hiệu KHOA HỌC khi số mũ hiệu chỉnh
    nhỏ hơn -6. Đã đo, cùng một cột `numeric`:

        nguồn 0.00000000000000000001  ->  ::text '0.00000000000000000001'
                                      ->  str(Decimal) '1E-20'
        nguồn 1e-10                   ->  ::text '0.0000000001'
                                      ->  str(Decimal) '1E-10'

    Tức là chú thích "bronze giữ nguyên chuỗi thập phân nguồn" ở
    `_ARROW_TYPE_MAP` chỉ đúng với những con số ai đó tình cờ đã thử — cùng
    đúng một lối hỏng mà cả thay đổi này tồn tại để sửa. Sau đây nó đúng theo
    nghĩa đen. (Mười lăm dạng `numeric` khác đã kiểm cho ra chuỗi Y HỆT nhau ở
    hai đường, kể cả `NaN`, đuôi số 0 như `19.90`, và 39 chữ số — nên đây là
    một phép SỬA, không phải một phép đánh đổi.)

    **json/jsonb.** psycopg 3 mặc định PARSE cả hai thành `dict`/`list`, và khi
    đã thành `dict` thì văn bản gốc mất hẳn: `json.dumps` dựng lại được một
    chuỗi HỢP LỆ nhưng không phải chuỗi cũ. Đã đo — `{"b": 1, "a": 2.50}` đi
    một vòng parse/serialise ra `{"b": 1, "a": 2.5}`: mất `2.50`, và với kiểu
    `json` mất cả thứ tự khoá lẫn khoảng trắng. `::text` không đi vòng đó: với
    `json` nó trả về ĐÚNG văn bản đã lưu (đã đo, khớp từng byte, kể cả escape
    `\\u00e9` giữ nguyên); với `jsonb` nó trả về dạng chuẩn tắc mà chính server
    giữ — khoá đã sắp, `2.50` còn nguyên, vì jsonb lưu số dưới dạng numeric.
    Bronze là nơi đổ THÔ; sắp xếp lại là việc của silver.

    Lựa chọn còn lại cho json là `psycopg.types.json.set_json_loads(<giải mã>,
    conn)` — bảo psycopg đừng parse. Đã kiểm trên psycopg 3.3.4: nó CHẠY, nhận
    đúng phạm vi một connection (một connection mới vẫn trả `dict`, không rò
    trạng thái toàn cục), và sống qua cả cursor có tên. Không chọn nó, và lý do
    không phải kỹ thuật: nó là một CƠ CHẾ THỨ HAI cho cùng một việc. `::text`
    dù sao cũng phải tồn tại cho mảng, `inet`, `interval`, `bytea`; thêm một
    đường riêng cho json là dựng lại đúng hình dạng "hai chỗ phải khớp nhau"
    vừa bị xoá đi ở trên.

    Điều kiện là kiểu ARROW, không phải một danh sách tên kiểu: mọi thứ về
    `pa.string()` đều cần một `str`, và đó đúng là điều `::text` bảo đảm — kể
    cả cho kiểu chưa có trong `_ARROW_TYPE_MAP`, tức là kiểu chưa ai gặp.
    """
    return pg_type not in _NATIVE_TEXT_TYPES and pa.types.is_string(_arrow_type_for(pg_type))


def _parse_stream(stream: str) -> tuple[str, str]:
    parts = stream.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"stream phải có dạng 'schema.table', nhận {stream!r}")
    return parts[0], parts[1]


@dataclass(frozen=True, slots=True)
class _ColumnInfo:
    name: str
    arrow_type: pa.DataType
    nullable: bool
    # Postgres kết xuất cột này ra text ngay trong câu SELECT — xem
    # `_needs_text_cast`. Mang theo CỘT chứ không tính lại ở `_read_rows`: chỉ
    # `_columns_for` cầm chuỗi `data_type` của nguồn, và tính lại ở nơi khác là
    # tạo ra chỗ thứ hai phải khớp với chỗ thứ nhất.
    text_cast: bool


def _rows_to_record_batch(
    rows: list[dict[str, object]], columns: tuple[_ColumnInfo, ...]
) -> pa.RecordBatch:
    """Không còn bước ép kiểu nào ở đây, và sự VẮNG MẶT đó là điều đáng nói:
    mọi giá trị tới được chỗ này đã đúng dạng pyarrow nhận, vì `_needs_text_cast`
    đã quyết định điều đó từ lúc dựng câu SELECT. `pa.array` vẫn ném
    `ArrowTypeError`/`ArrowInvalid` nếu một ngày điều đó không còn đúng — nó là
    phép canh cuối cùng, không phải chỗ để vá thêm một nhánh `isinstance`."""
    fields = [pa.field(c.name, c.arrow_type, nullable=c.nullable) for c in columns]
    arrays = [pa.array([row[c.name] for row in rows], type=c.arrow_type) for c in columns]
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
                name=name,
                arrow_type=_arrow_type_for(pg_type),
                nullable=(is_nullable == "YES"),
                text_cast=_needs_text_cast(pg_type),
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
            # Cột nào cần Postgres kết xuất ra text (xem `_needs_text_cast`)
            # mang `::text` NGAY TRONG câu SELECT, và được ĐẶT LẠI ĐÚNG TÊN CŨ
            # bằng `AS`: `dict_row` lấy khoá của mỗi dòng từ tên cột KẾT QUẢ,
            # nên thiếu alias là `row[c.name]` ở `_rows_to_record_batch` ném
            # `KeyError` cho mọi cột được cast.
            #
            # `WHERE`/`ORDER BY` phía dưới không bị ảnh hưởng, và điều đó đúng
            # vì một lý do cụ thể chứ không phải may: cột cursor chỉ có thể
            # mang một trong sáu kiểu của `CURSOR_TYPE_ALLOWLIST` (nguyên,
            # ngày, hai kiểu timestamp), không kiểu nào trong sáu về
            # `pa.string()`, nên không kiểu nào bị cast. So sánh watermark vì
            # thế vẫn là so sánh SỐ/NGÀY phía Postgres, không phải so sánh
            # chuỗi — đúng thứ `loom_core.cursor` tồn tại để ngăn.
            # `test_no_cursor_type_is_ever_read_as_text` canh mệnh đề đó.
            select_list = sql.SQL(", ").join(
                sql.SQL("{0}::text AS {0}").format(sql.Identifier(c.name))
                if c.text_cast
                else sql.Identifier(c.name)
                for c in columns
            )
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
