"""Test riêng của `PostgresConnector`.

Bộ hợp đồng dùng chung (`packages/connectorkit/tests/test_connector_contract.py`)
đã canh bảy hành vi mà MỌI `Connector` phải có, chạy giống hệt trên `fake` lẫn
`postgres`. Bộ này canh những gì CHỈ Postgres mới có: cursor phía server thật,
ép kiểu NUMERIC/timestamptz thật qua information_schema thật, `check()` trên
một host không nghe (không phải một fake luôn trả `ok=False` theo yêu cầu).

**`type_zoo` và vì sao nó ở TRONG file này, không ở `conftest.py`.** Bảng
`type_zoo` cùng bảng kỳ vọng `_TYPE_ZOO` là một khối duy nhất: cột nào, giá trị
SQL nào, kiểu Arrow nào, chuỗi nào đọc ra. Tách DDL sang conftest và kỳ vọng
sang đây là dựng lại đúng hình dạng đã gây ra sự cố mà bộ này tồn tại để chặn —
hai chỗ mô tả một hợp đồng, không gì bắt chúng khớp (xem `_needs_text_cast`
trong `postgres.py`). `source_dsn` vẫn thừa kế từ `tests/conftest.py` bình
thường; conftest tồn tại để CHIA SẺ fixture, và fixture này không chia sẻ với
ai.
"""

from __future__ import annotations

import resource
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime

import psycopg
import pyarrow as pa
import pytest
from psycopg import sql

from loom_connector.postgres import _ARROW_TYPE_MAP, PostgresConnector, _needs_text_cast
from loom_connector.protocol import StreamState
from loom_core.cursor import CURSOR_TYPE_ALLOWLIST, parse_cursor_value

pytestmark = pytest.mark.integration

# Đã đo bằng script rời (không giữ trong repo) trên CÙNG hình dạng dữ liệu mà
# `large_seeded_source` dựng (~50 MB payload, lấy đúng một lô 100 dòng):
# cursor có tên tăng RSS tiến trình +0.4 MiB, cursor thường (bỏ `name=`) tăng
# +98.8 MiB cho CÙNG một lô. Ngưỡng dưới đây nằm giữa hai con số đó với biên
# rộng cả hai phía (~50 lần trên mức "named cursor thật", ~5 lần dưới mức
# "cursor thường") để không nhạy với dao động RSS bình thường của tiến trình
# Python (import, GC, ...) nhưng vẫn bắt được đúng lớp lỗi cần bắt.
_MATERIALIZE_THRESHOLD_MIB = 20.0


def test_check_reports_ok_for_a_reachable_source(source_dsn: str) -> None:
    result = PostgresConnector(dsn=source_dsn).check()
    assert result.ok is True
    assert result.message != ""


def test_check_returns_failure_instead_of_raising_for_an_unreachable_host() -> None:
    """`check()` là điều UI gọi sau nút "Test connection" (spec Giai đoạn 3c)
    — một stack trace không phải một thông báo người vận hành đọc được.

    Cổng 1 trên loopback cần quyền root để bind nên chắc chắn không ai đang
    lắng nghe ở đó — kết nối bị HỆ ĐIỀU HÀNH từ chối ngay lập tức (ECONNREFUSED),
    không cần chờ timeout nào, nên bài này không tốn thời gian chờ.
    """
    connector = PostgresConnector(dsn="postgresql://nobody:nobody@127.0.0.1:1/nope")
    result = connector.check()
    assert result.ok is False
    assert result.message != ""


def test_discover_finds_the_table_columns_and_right_candidate_cursors(
    seeded_source: tuple[str, str, int, int],
) -> None:
    dsn, stream_name, _, _ = seeded_source
    connector = PostgresConnector(dsn=dsn)
    stream = next(s for s in connector.discover() if s.name == stream_name)
    assert {c.name for c in stream.columns} == {"id", "placed_at", "amount", "note"}
    # `amount` là NUMERIC — sắp xếp được nhưng không đảm bảo tăng dần theo
    # thời gian chèn. `note` là TEXT — sắp theo từ điển, không theo thời gian.
    # Chỉ còn "id" và "placed_at" là watermark AN TOÀN.
    #
    # Khẳng định cả KIỂU, không chỉ tên: chuỗi kiểu này đi nguyên văn tới
    # `/internal/ingest/{run_id}/progress` và bị `CURSOR_TYPE_ALLOWLIST` kiểm ở
    # đó (xem `CursorCandidate`), nên nó phải là tên của `information_schema`
    # (`timestamp with time zone`) chứ không phải tên `pg_catalog` (`timestamptz`
    # — đúng chữ mà `CREATE TABLE` trong fixture dùng).
    assert {(c.name, c.cursor_type) for c in stream.candidate_cursors} == {
        ("id", "integer"),
        ("placed_at", "timestamp with time zone"),
    }


def test_nullable_is_read_from_the_source_not_guessed(
    seeded_source: tuple[str, str, int, int],
) -> None:
    dsn, stream_name, _, _ = seeded_source
    connector = PostgresConnector(dsn=dsn)
    stream = next(s for s in connector.discover() if s.name == stream_name)
    by_name = {c.name: c for c in stream.columns}
    assert by_name["note"].nullable is True
    assert by_name["id"].nullable is False
    assert by_name["placed_at"].nullable is False
    assert by_name["amount"].nullable is False


def test_read_produces_the_expected_number_of_batches(
    seeded_source: tuple[str, str, int, int],
) -> None:
    dsn, stream_name, n_rows, batch_rows = seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)
    batches = list(connector.read(stream_name, StreamState()))
    expected_batches = (n_rows + batch_rows - 1) // batch_rows  # chia lấy trần
    assert len(batches) == expected_batches
    assert sum(b.num_rows for b in batches) == n_rows


def test_read_does_not_materialize_before_the_first_batch(
    large_seeded_source: tuple[str, str, int],
) -> None:
    """Đây là bài quan trọng nhất của file này — nó canh đúng thứ cursor CÓ
    TÊN tồn tại để ngăn (xem docstring đầu `postgres.py`).

    Một bản cài gọi `fetchall()` (hay dùng cursor THƯỜNG rồi `fetchmany()`
    sau đó) vẫn cho ra ĐÚNG SỐ LƯỢNG batch và ĐÚNG SỐ DÒNG mỗi batch — đếm
    batch không phân biệt được hai cách cài đặt, vì cursor thường của psycopg
    đã kéo hết bảng về RAM CLIENT ngay trong `execute()`, trước khi
    `fetchmany()` đầu tiên chạy, và `fetchmany()` sau đó chỉ cắt từ bộ nhớ đã
    có sẵn — bên ngoài không thấy khác biệt gì qua giá trị trả về.

    Cách bắt: đo RSS tiến trình (`ru_maxrss` — mốc CAO NHẤT, không giảm khi
    RAM được giải phóng, cùng lý lẽ với `scripts/measure_ingest_pod.py`) ngay
    trước khi gọi `read()` và ngay sau khi lấy được đúng MỘT batch (không gọi
    `list()`). Với cursor có tên, chỉ 100 dòng đã về tới Python — chênh lệch
    nhỏ. Với cursor thường, cả ~50 MB payload đã về tới Python trước đó —
    chênh lệch lớn hơn ngưỡng `_MATERIALIZE_THRESHOLD_MIB` nhiều lần.
    """
    dsn, stream_name, batch_rows = large_seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)

    before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = connector.read(stream_name, StreamState())
    first_batch = next(result)
    after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert first_batch.num_rows == batch_rows
    delta_mib = (after_kib - before_kib) / 1024
    assert delta_mib < _MATERIALIZE_THRESHOLD_MIB, (
        f"RSS tăng {delta_mib:.1f} MiB chỉ để lấy MỘT lô {batch_rows} dòng trên "
        "một bảng ~50 MB — nghi ngờ read() đã kéo hết bảng về client thay vì "
        "dùng cursor phía server"
    )


def test_incremental_read_includes_the_boundary_row(
    seeded_source: tuple[str, str, int, int],
) -> None:
    """Lọc `>=`, KHÔNG `>` — xem docstring `_read_rows` trong `postgres.py`.
    Dòng có `id` ĐÚNG BẰNG mốc watermark bắt buộc phải có mặt lại sau resume."""
    dsn, stream_name, n_rows, _ = seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=n_rows)
    full = list(connector.read(stream_name, StreamState()))
    ids = sorted(v for b in full for v in b.column("id").to_pylist())
    boundary = ids[len(ids) // 2]

    resumed = list(
        connector.read(stream_name, StreamState(cursor_column="id", cursor_value=str(boundary)))
    )
    resumed_ids = [v for b in resumed for v in b.column("id").to_pylist()]
    assert resumed_ids, "lọc theo cursor không được trả rỗng khi còn dòng phía sau mốc"
    assert min(resumed_ids) == boundary
    assert boundary in resumed_ids, (
        f"dòng id={boundary} (mốc watermark) bị thiếu sau khi resume — lọc đang "
        "là '>' chứ không phải '>=', nghĩa là dữ liệu mất thật"
    )


def test_arrow_types_survive_the_round_trip(seeded_source: tuple[str, str, int, int]) -> None:
    dsn, stream_name, n_rows, _ = seeded_source
    connector = PostgresConnector(dsn=dsn, batch_rows=n_rows)
    batches = list(connector.read(stream_name, StreamState()))
    table = pa.Table.from_batches(batches)

    assert pa.types.is_integer(table.schema.field("id").type)
    assert pa.types.is_timestamp(table.schema.field("placed_at").type)
    # NUMERIC -> Arrow string, KHÔNG float64 — xem docstring `postgres.py`.
    assert pa.types.is_string(table.schema.field("amount").type)

    ids = table.column("id").to_pylist()
    amounts = table.column("amount").to_pylist()
    idx = ids.index(0)
    assert amounts[idx] == "19.99", (
        "NUMERIC phải giữ ĐÚNG chuỗi thập phân đã insert, không được ép qua "
        "float rồi định dạng lại (đó là đúng lớp lỗi mất chính xác đã bị cấm)"
    )


# ---------------------------------------------------------------------------
# Phủ kiểu: MỌI kiểu trong `_ARROW_TYPE_MAP` + những kiểu KHÔNG có trong đó
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ZooColumn:
    """Một cột của bảng `type_zoo` và mọi thứ phép canh cần biết về nó.

    `data_type` được VIẾT RA ở đây chứ không đọc từ nguồn rồi so với chính nó:
    nó là chuỗi `information_schema.columns.data_type` mà `_arrow_type_for` tra
    bảng bằng, nên viết ra là cách duy nhất để
    `test_information_schema_reports_the_data_type_the_zoo_declares` bắt được
    một chuỗi đoán sai (`'ARRAY'` và `'USER-DEFINED'` viết HOA, `time` thật ra
    là `'time without time zone'` — không ai nhớ đúng cả ba).
    """

    column: str
    ddl: str
    literal: str
    data_type: str
    arrow_type: pa.DataType
    expected: object


_ZOO_SCHEMA = "loom_type_zoo_test"
_ZOO_STREAM = f"{_ZOO_SCHEMA}.type_zoo"

# Mỗi dòng dưới đây là một phép ĐO đã chạy trên Postgres 17, không phải một
# phỏng đoán — xem bảng số liệu ở `_arrow_type_for` và `_needs_text_cast`.
_TYPE_ZOO: tuple[_ZooColumn, ...] = (
    # --- có trong `_ARROW_TYPE_MAP`, về kiểu Arrow đúng nghĩa ---
    _ZooColumn("c_smallint", "smallint", "32767", "smallint", pa.int16(), 32767),
    _ZooColumn("c_integer", "integer", "2147483647", "integer", pa.int32(), 2147483647),
    _ZooColumn(
        "c_bigint",
        "bigint",
        "9223372036854775807",
        "bigint",
        pa.int64(),
        9223372036854775807,
    ),
    _ZooColumn("c_real", "real", "1.5", "real", pa.float32(), 1.5),
    _ZooColumn("c_double", "double precision", "1.5", "double precision", pa.float64(), 1.5),
    _ZooColumn("c_boolean", "boolean", "true", "boolean", pa.bool_(), True),
    _ZooColumn("c_date", "date", "'2024-01-02'", "date", pa.date32(), date(2024, 1, 2)),
    _ZooColumn(
        "c_timestamp",
        "timestamp without time zone",
        "'2024-01-02 03:04:05.123456'",
        "timestamp without time zone",
        pa.timestamp("us"),
        datetime(2024, 1, 2, 3, 4, 5, 123456),
    ),
    _ZooColumn(
        "c_timestamptz",
        "timestamp with time zone",
        "'2024-01-02 03:04:05.123456+00'",
        "timestamp with time zone",
        pa.timestamp("us", tz="UTC"),
        datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
    ),
    # --- có trong `_ARROW_TYPE_MAP`, về string ---
    # Khoảng trắng ĐUÔI có thật trong dữ liệu: `text`/`varchar` phải giữ nó.
    _ZooColumn("c_text", "text", "'hello '", "text", pa.string(), "hello "),
    _ZooColumn(
        "c_varchar", "character varying(20)", "'hello '", "character varying", pa.string(), "hello "
    ),
    # `character(8)` là lý do `_NATIVE_TEXT_TYPES` tồn tại: đệm đuôi phải CÒN.
    # Một bản cài đặt cast cả bpchar sang text sẽ đọc ra `'abc'` — Postgres cắt
    # khoảng trắng đệm trong phép ép bpchar->text.
    _ZooColumn("c_char", "character(8)", "'abc'", "character", pa.string(), "abc     "),
    # Chữ HOA đi vào, chữ thường chuẩn tắc đi ra — uuid không có thế lưỡng nan
    # nào như json: dạng chuẩn tắc là dạng duy nhất và không mất gì.
    _ZooColumn(
        "c_uuid",
        "uuid",
        "'0B7F9E6A-1C2D-4E3F-8A9B-0C1D2E3F4A5B'",
        "uuid",
        pa.string(),
        "0b7f9e6a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
    ),
    # `json` giữ ĐÚNG văn bản nguồn: thứ tự khoá `b` trước `a`, `2.50` chưa bị
    # chuẩn hoá thành `2.5`, `[1,2]` chưa bị chèn khoảng trắng. Một vòng
    # `json.dumps(json.loads(...))` làm mất cả ba.
    _ZooColumn(
        "c_json",
        "json",
        """'{"b": 1, "a": 2.50, "c": [1,2]}'""",
        "json",
        pa.string(),
        '{"b": 1, "a": 2.50, "c": [1,2]}',
    ),
    # `jsonb` KHÔNG giữ văn bản nguồn — chính SERVER chuẩn hoá nó khi lưu (sắp
    # khoá, chuẩn hoá khoảng trắng). Chuỗi dưới đây là dạng chuẩn tắc đó, tức
    # là "cái nguồn thật sự đang giữ", và `2.50` vẫn còn nguyên vì jsonb lưu số
    # dưới dạng numeric.
    _ZooColumn(
        "c_jsonb",
        "jsonb",
        """'{"b": 1, "a": 2.50, "c": [1,2]}'""",
        "jsonb",
        pa.string(),
        '{"a": 2.50, "b": 1, "c": [1, 2]}',
    ),
    _ZooColumn("c_numeric", "numeric(12,2)", "19.99", "numeric", pa.string(), "19.99"),
    # Con số này là cả lý do cột thứ hai tồn tại: `str(decimal.Decimal)` cho
    # `'1E-20'` (ký hiệu khoa học khi số mũ hiệu chỉnh < -6), còn nguồn giữ
    # `0.00000000000000000001`. Bản trước đọc ra `'1E-20'` — im lặng, không lỗi.
    _ZooColumn(
        "c_numeric_small",
        "numeric",
        "0.00000000000000000001",
        "numeric",
        pa.string(),
        "0.00000000000000000001",
    ),
    # --- KHÔNG có trong `_ARROW_TYPE_MAP`: nhánh dễ dãi của `_arrow_type_for` ---
    _ZooColumn("c_int_array", "integer[]", "'{1,2,3}'", "ARRAY", pa.string(), "{1,2,3}"),
    _ZooColumn("c_text_array", "text[]", """'{"a","b"}'""", "ARRAY", pa.string(), "{a,b}"),
    # `/32` KHÔNG thừa: Postgres có một phép ép `inet -> text` riêng
    # (`network_show`) luôn in kèm độ dài tiền tố, khác với cách nó hiển thị
    # một giá trị inet. Chuỗi này đọc ngược vào một cột inet cho đúng giá trị
    # cũ, nên nó vẫn là "cái nguồn giữ" — chỉ là nói đầy đủ hơn.
    _ZooColumn("c_inet", "inet", "'192.168.1.1'", "inet", pa.string(), "192.168.1.1/32"),
    _ZooColumn(
        "c_interval", "interval", "'1 day 02:03:04'", "interval", pa.string(), "1 day 02:03:04"
    ),
    _ZooColumn("c_time", "time", "'03:04:05'", "time without time zone", pa.string(), "03:04:05"),
    _ZooColumn("c_bytea", "bytea", r"'\xdeadbeef'::bytea", "bytea", pa.string(), r"\xdeadbeef"),
    _ZooColumn("c_enum", "zoo_mood", "'happy'", "USER-DEFINED", pa.string(), "happy"),
    # `oid` là ca đáng nhớ nhất của nhánh dễ dãi: psycopg trả một `int` HOÀN
    # TOÀN bình thường, và pyarrow vẫn từ chối nó trước một mảng string. Nên
    # "kiểu lạ" không phải điều kiện gây hỏng.
    _ZooColumn("c_oid", "oid", "42", "oid", pa.string(), "42"),
)


@pytest.fixture(scope="module")
def type_zoo(source_dsn: str) -> Iterator[tuple[str, str]]:
    """Bảng MỘT cột cho mỗi mục của `_TYPE_ZOO`, hai dòng: `k=1` mang giá trị,
    `k=2` để NULL hết. Trả `(dsn, stream_name)`.

    Dòng NULL không phải phần thừa: `NULL` đi qua `::text` vẫn là `NULL`, và
    một bản cài đặt ép kiểu ở Python bằng `str(value)` sẽ biến nó thành chuỗi
    `'None'` — mất khả năng phân biệt "không có dữ liệu" với "có dữ liệu là
    chữ None", đúng loại hỏng âm thầm không để lại dấu vết.
    """
    with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_ZOO_SCHEMA)))
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(_ZOO_SCHEMA)))
        # `search_path` để `zoo_mood` trong DDL của `_TYPE_ZOO` không phải mang
        # theo tên schema — nếu mang, tên schema bị chép thành hằng số thứ hai
        # bên cạnh `_ZOO_SCHEMA`.
        cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(_ZOO_SCHEMA)))
        cur.execute(sql.SQL("CREATE TYPE zoo_mood AS ENUM ('happy', 'sad')"))
        columns = sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(z.column), sql.SQL(z.ddl)) for z in _TYPE_ZOO
        )
        cur.execute(
            sql.SQL("CREATE TABLE {}.type_zoo (k integer NOT NULL, {})").format(
                sql.Identifier(_ZOO_SCHEMA), columns
            )
        )
        literals = sql.SQL(", ").join(sql.SQL(z.literal) for z in _TYPE_ZOO)
        cur.execute(
            sql.SQL("INSERT INTO {}.type_zoo VALUES (1, {})").format(
                sql.Identifier(_ZOO_SCHEMA), literals
            )
        )
        cur.execute(
            sql.SQL("INSERT INTO {}.type_zoo (k) VALUES (2)").format(sql.Identifier(_ZOO_SCHEMA))
        )
    yield source_dsn, _ZOO_STREAM
    with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_ZOO_SCHEMA)))


def test_the_type_zoo_covers_every_mapped_type() -> None:
    """Phép canh CHỐNG TRÔI, và là lý do cả khối này tồn tại.

    Sự cố gốc: `_ARROW_TYPE_MAP` khai `uuid`/`json`/`jsonb` về string, còn
    đường ép giá trị chỉ biết `decimal.Decimal` — hai chỗ mô tả một hợp đồng,
    không gì bắt chúng khớp, và chỗ lệch chỉ lộ ra ở lần nạp THẬT đầu tiên gặp
    một cột uuid. Bài này biến "thêm một kiểu vào bảng mà quên kiểm nó" thành
    một phép kiểm ĐỎ ngay tại chỗ, thay vì một `ArrowTypeError` ở production.
    """
    covered = {z.data_type for z in _TYPE_ZOO}
    missing = sorted(set(_ARROW_TYPE_MAP) - covered)
    assert not missing, (
        f"kiểu {missing} có trong _ARROW_TYPE_MAP nhưng không có cột nào trong "
        "_TYPE_ZOO mang kiểu đó — nghĩa là không gì kiểm nó thật sự đọc được. "
        "Thêm một _ZooColumn cho từng kiểu, đừng nới phép canh này."
    )


def test_information_schema_reports_the_data_type_the_zoo_declares(
    type_zoo: tuple[str, str],
) -> None:
    """`_arrow_type_for` tra bảng bằng chuỗi `information_schema.columns.
    data_type`, nên một chuỗi viết sai trong `_TYPE_ZOO` sẽ làm mọi bài dưới
    kiểm nhầm nhánh mà vẫn xanh. Đối chiếu với nguồn thật ở đây, một lần."""
    dsn, _ = type_zoo
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'type_zoo'",
            (_ZOO_SCHEMA,),
        )
        actual = dict(cur.fetchall())
    assert {z.column: z.data_type for z in _TYPE_ZOO} == {
        z.column: actual[z.column] for z in _TYPE_ZOO
    }


def test_every_mapped_type_round_trips(type_zoo: tuple[str, str]) -> None:
    """MỌI kiểu của `_ARROW_TYPE_MAP` cộng những kiểu KHÔNG có trong đó, đẩy
    qua một Postgres thật, phải ra một `RecordBatch` với ĐÚNG giá trị.

    Khẳng định cả giá trị chứ không chỉ "không ném": một bản cài đặt ép kiểu
    bằng `str()` phía Python cũng không ném — nó chỉ ghi `'[1, 2, 3]'` thay vì
    `'{1,2,3}'` vào bronze, và không ai biết cho tới lúc đọc lại.
    """
    dsn, stream_name = type_zoo
    connector = PostgresConnector(dsn=dsn, batch_rows=10)
    # `cursor_column="k"` chỉ để có ORDER BY — cần thứ tự xác định để biết dòng
    # nào là dòng mang giá trị và dòng nào là dòng NULL.
    batches = list(connector.read(stream_name, StreamState(cursor_column="k")))
    table = pa.Table.from_batches(batches)
    assert table.num_rows == 2

    for z in _TYPE_ZOO:
        field = table.schema.field(z.column)
        assert field.type == z.arrow_type, (
            f"{z.column} ({z.data_type}) ra kiểu Arrow {field.type}, chờ {z.arrow_type}"
        )
        values = table.column(z.column).to_pylist()
        assert values[0] == z.expected, (
            f"{z.column} ({z.data_type}) đọc ra {values[0]!r}, chờ {z.expected!r}"
        )
        assert values[1] is None, (
            f"{z.column} ({z.data_type}) biến NULL thành {values[1]!r} — NULL của "
            "nguồn phải còn là null trong Arrow, không được thành một chuỗi"
        )


def test_no_cursor_type_is_ever_read_as_text() -> None:
    """Cột cursor đi vào `WHERE {} >= %s` và `ORDER BY {}` với tên CỘT GỐC, còn
    danh sách SELECT có thể mang `::text`. Hai chỗ đó chỉ không đá nhau chừng
    nào không kiểu cursor nào bị cast — nếu một kiểu cursor bị cast, `ORDER BY`
    trên một bare name sẽ ưu tiên cột KẾT QUẢ (đã là text) và phép so sánh
    watermark âm thầm trở thành so sánh CHUỖI, đúng thứ `loom_core.cursor` tồn
    tại để ngăn. Mệnh đề đó được canh ở đây thay vì được tin."""
    cast = sorted(t for t in CURSOR_TYPE_ALLOWLIST if _needs_text_cast(t))
    assert not cast, (
        f"kiểu cursor {cast} bị đọc dưới dạng text — so sánh watermark sẽ thành so sánh chuỗi"
    )


def test_every_allowed_cursor_type_has_an_arrow_mapping() -> None:
    """`CURSOR_TYPE_ALLOWLIST` (loom_core) và `_ARROW_TYPE_MAP` (connector) là
    hai danh sách khác nhau viết ở hai package khác nhau, nhưng cả hai đánh chỉ
    mục bằng CÙNG chuỗi `information_schema.columns.data_type`. Một kiểu được
    phép làm cursor mà connector không biết ánh xạ sẽ rơi vào nhánh dễ dãi và
    về string — tức là `discover()` đề xuất một cursor mà chính nó đọc ra dạng
    text."""
    unmapped = sorted(CURSOR_TYPE_ALLOWLIST - set(_ARROW_TYPE_MAP))
    assert not unmapped, (
        f"kiểu cursor {unmapped} được loom-api chấp nhận nhưng connector không "
        "có ánh xạ Arrow cho nó"
    )


def test_discover_offers_only_cursor_types_the_control_plane_accepts(
    type_zoo: tuple[str, str],
) -> None:
    """`type_zoo` có đủ cả sáu kiểu của `CURSOR_TYPE_ALLOWLIST` và mười bảy kiểu
    ngoài nó, nên nó là chỗ tốt hơn `orders` để canh phép lọc: mỗi
    `cursor_type` đề xuất phải qua được `parse_cursor_value` — đúng hàm mà
    `loom-api` chạy khi pod nạp báo tiến độ về (`routers/internal_ingest.py`).
    Một chuỗi kiểu lọt qua đây mà `loom-api` từ chối nghĩa là watermark không
    bao giờ tiến, và không có lỗi nào ở phía connector để lần ra."""
    dsn, stream_name = type_zoo
    stream = next(s for s in PostgresConnector(dsn=dsn).discover() if s.name == stream_name)
    offered = {c.cursor_type for c in stream.candidate_cursors}
    assert offered == CURSOR_TYPE_ALLOWLIST, (
        "type_zoo mang đủ sáu kiểu hợp lệ nên discover() phải đề xuất đủ sáu, "
        f"không nhiều hơn — nhận {sorted(offered)}"
    )
    zoo_columns = {z.column for z in _TYPE_ZOO} | {"k"}
    for candidate in stream.candidate_cursors:
        assert candidate.name in zoo_columns
        # Giá trị mẫu hợp lệ cho từng kiểu, đi qua đúng hàm loom-api dùng.
        sample = {
            "smallint": "1",
            "integer": "1",
            "bigint": "1",
            "date": "2024-01-02",
            "timestamp without time zone": "2024-01-02T03:04:05",
            "timestamp with time zone": "2024-01-02T03:04:05+00:00",
        }[candidate.cursor_type]
        parse_cursor_value(candidate.cursor_type, sample)
