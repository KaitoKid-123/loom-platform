"""Bộ hợp đồng dùng chung cho MỌI cài đặt `Connector` — đây là spec v1 §9.

Sao chỉ một cài đặt (`PostgresConnector`, Task 5) thì bộ này không chứng minh
được điều nó tuyên bố: nó có thể đang mã hoá THÓI QUEN của Postgres chứ không
phải HỢP ĐỒNG của `Connector`, và vẫn xanh. Đó là kiểu "phép kiểm không thấy
được thứ nó gọi tên" mà dự án đã dính mười lăm lần — hai lần ngay trong giai
đoạn này. `FakeConnector` (packages/connectorkit/src/loom_connector/fake.py)
tồn tại CHỈ để giữ bộ này trung thực: nếu bộ chỉ `PostgresConnector` qua được,
nó đang mô tả Postgres, không phải `Connector`.

`params=` trên fixture `connector_factory` là điểm nối theo NGHĨA "mỗi
implementation tự đăng ký một dòng ở `_IMPLEMENTATIONS`". Task 5 thêm
`postgres` vào danh sách đó, nhưng — khác với dự tính ban đầu của đoạn này —
không CHỈ thêm một dòng: `postgres` cần một fixture thật (`source_dsn`, một
container Postgres), mà một hàm module-level thuần tuý như `_fake_builder` cũ
không có cách nào xin được, nên `_register`/`connector_factory` bên dưới nhận
thêm `request: pytest.FixtureRequest` để `_postgres_builder` gọi
`request.getfixturevalue("source_dsn")`; và `postgres` cần đánh dấu
`pytest.mark.integration` (nó đòi Docker, `fake` thì không) để `make test`
mặc định (`-m 'not integration'`) không đột nhiên cần Docker. Cả hai là độ
rộng tối thiểu cần thêm để giữ đúng phần quan trọng của lời hứa gốc: KHÔNG
một bài test nào bên dưới (`test_check_...` trở xuống) bị sửa.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Protocol

import psycopg
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from psycopg import sql

from loom_connector.protocol import Connector, StreamState

# ConnectorFactory: (số dòng muốn có) -> Connector sẵn sàng dùng. Mỗi cài đặt
# tự quyết "số dòng" nghĩa là gì (bảng in-memory, bảng Postgres đã seed, ...) —
# bộ hợp đồng không cần biết, nó chỉ cần MỘT connector đã có dữ liệu để đọc.


class ConnectorFactory(Protocol):
    def __call__(self, n_rows: int) -> Connector: ...


# FactoryBuilder: (request của CHÍNH test đang chạy) -> ConnectorFactory.
# `FakeConnector` không cần gì từ bên ngoài nên hàm dựng của nó bỏ qua
# `request`. `PostgresConnector` cần `source_dsn` — một fixture — và
# `request.getfixturevalue` là cách DUY NHẤT một hàm module-level thuần tuý
# (không tự là fixture) xin được giá trị của một fixture khác.
class FactoryBuilder(Protocol):
    def __call__(self, request: pytest.FixtureRequest) -> ConnectorFactory: ...


_IMPLEMENTATIONS: list[tuple[str, FactoryBuilder, tuple[pytest.MarkDecorator, ...]]] = []


def _register(
    name: str, builder: FactoryBuilder, marks: tuple[pytest.MarkDecorator, ...] = ()
) -> None:
    _IMPLEMENTATIONS.append((name, builder, marks))


def _fake_builder(request: pytest.FixtureRequest) -> ConnectorFactory:
    from loom_connector.fake import FakeConnector

    def make(n_rows: int) -> Connector:
        return FakeConnector(n_rows=n_rows, batch_size=7)

    return make


_register("fake", _fake_builder)


# Schema RIÊNG cho bộ hợp đồng, tách khỏi schema mà
# `tests/integration/test_postgres_connector.py` dùng — cả hai đứng trên
# CÙNG một container (`source_dsn`, session-scoped, xem `tests/conftest.py`)
# mà không giẫm lên bảng của nhau, nhờ `PostgresConnector(schema=...)` giới
# hạn `discover()` vào đúng một namespace.
_CONTRACT_SCHEMA = "loom_contract"
_CONTRACT_TABLE = "seed"


def _reset_contract_table(dsn: str, n_rows: int) -> None:
    """Dọn và dựng lại MỘT bảng CỐ ĐỊNH TÊN trong schema riêng của bộ hợp đồng.

    Tên cố định, KHÔNG một tên mới mỗi lần gọi: `PostgresConnector.discover()`
    trả MỌI bảng trong schema nó được cấu hình, và các bài trong bộ hợp đồng
    (`test_read_yields_record_batches` và các bài sau) gọi
    `connector.discover()[0]` — giả định đúng MỘT stream nhìn thấy được. Một
    tên mới mỗi lần factory được gọi sẽ để lại bảng cũ từ lần gọi trước, biến
    giả định "đúng một stream" thành sai kể từ lần gọi thứ hai của CHÍNH bộ
    test này (không phải do đụng bộ test khác — schema riêng đã cách ly việc
    đó).

    Cột khớp hình dạng `FakeConnector` (`id`, `updated_at` cùng tăng dần,
    `payload` nullable) để cùng một `cursor = stream.candidate_cursors[0]`
    chạy đúng logic `>=` trên cả hai implementation.
    """
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_CONTRACT_SCHEMA))
        )
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(_CONTRACT_SCHEMA)))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.{} ("
                "id integer NOT NULL, "
                "updated_at timestamptz NOT NULL, "
                "payload text)"
            ).format(sql.Identifier(_CONTRACT_SCHEMA), sql.Identifier(_CONTRACT_TABLE))
        )
        base = datetime(2024, 1, 1, tzinfo=UTC)
        cur.executemany(
            sql.SQL("INSERT INTO {}.{} (id, updated_at, payload) VALUES (%s, %s, %s)").format(
                sql.Identifier(_CONTRACT_SCHEMA), sql.Identifier(_CONTRACT_TABLE)
            ),
            [
                (i, base + timedelta(seconds=i), None if i % 5 == 0 else f"payload-{i}")
                for i in range(n_rows)
            ],
        )


def _postgres_builder(request: pytest.FixtureRequest) -> ConnectorFactory:
    from loom_connector.postgres import PostgresConnector

    dsn: str = request.getfixturevalue("source_dsn")

    def make(n_rows: int) -> Connector:
        _reset_contract_table(dsn, n_rows)
        return PostgresConnector(dsn=dsn, schema=_CONTRACT_SCHEMA, batch_rows=7)

    return make


# Cần Docker (testcontainers) nên đánh dấu integration: `make test` mặc định
# (`-m 'not integration'`) chạy bộ này chỉ với `fake`; `make test-int` (đè
# addopts) chạy với CẢ HAI — đúng lời hứa của bộ hợp đồng là chứng minh cho
# nhiều hơn một implementation, không chỉ ở một CI riêng của integration mà
# `make test` không bao giờ chạy tới.
_register("postgres", _postgres_builder, marks=(pytest.mark.integration,))


@pytest.fixture(
    params=[
        pytest.param(builder, marks=marks, id=name) for name, builder, marks in _IMPLEMENTATIONS
    ],
)
def connector_factory(request: pytest.FixtureRequest) -> ConnectorFactory:
    builder: FactoryBuilder = request.param
    return builder(request)


# Dữ liệu mặc định vừa đủ để có midpoint rõ ràng cho bài cursor `>=` (bài 6),
# và vừa đủ để laziness (bài 5) không bị nguỵ trang bởi dữ liệu quá nhỏ.
_DEFAULT_ROWS = 30


def test_check_reports_ok_with_a_non_empty_message(connector_factory: ConnectorFactory) -> None:
    """`check()` phải nói được điều gì đó ngay cả khi thành công.

    Một `CheckResult(ok=True, message="")` xanh cho MỌI phép kiểm tự động
    nhưng vô dụng trên UI: người vận hành bấm "Test connection" và không thấy
    gì để biết nó vừa xác nhận điều gì (kết nối được? đọc được bảng nào?).
    """
    connector = connector_factory(_DEFAULT_ROWS)
    result = connector.check()
    assert result.ok is True
    assert result.message != ""


def test_discover_returns_at_least_one_stream_with_columns(
    connector_factory: ConnectorFactory,
) -> None:
    """Một connector không có `discover()` dùng được thì `loom-api` không có
    gì để hiển thị cho người dùng chọn stream/cột — nó bắt buộc phải có ít
    nhất một stream, và stream đó phải có tên và cột thật, không phải khung
    rỗng."""
    connector = connector_factory(_DEFAULT_ROWS)
    streams = connector.discover()
    assert len(streams) >= 1
    for stream in streams:
        assert stream.name != ""
        assert len(stream.columns) > 0


def test_candidate_cursors_are_real_columns(connector_factory: ConnectorFactory) -> None:
    """Mỗi tên trong `candidate_cursors` phải là một cột THẬT của chính stream đó.

    Một cursor trỏ tới cột không tồn tại là cấu hình chết ngay từ lúc khai
    báo — nhưng nó không nổ ra ở `discover()`, nó nằm im cho tới lần đọc gia
    tăng (incremental) đầu tiên dùng cursor đó, tức là xa hẳn chỗ gây ra nó.
    Bài này bắt lỗi tại nguồn, lúc discover(), thay vì tại nơi nó phát tác.
    """
    connector = connector_factory(_DEFAULT_ROWS)
    for stream in connector.discover():
        column_names = {c.name for c in stream.columns}
        for cursor in stream.candidate_cursors:
            assert cursor in column_names, (
                f"stream '{stream.name}' khai báo candidate_cursor '{cursor}' "
                f"nhưng không có cột nào tên vậy trong {sorted(column_names)}"
            )


def test_read_yields_record_batches(connector_factory: ConnectorFactory) -> None:
    """`read()` phải sinh ra `pa.RecordBatch` — kiểu Arrow mà `loom-task` ghi
    thẳng xuống bronze, không phải dict hay pandas DataFrame cần chuyển đổi
    thêm ở tầng trên."""
    connector = connector_factory(_DEFAULT_ROWS)
    stream = connector.discover()[0]
    batches = list(connector.read(stream.name, StreamState()))
    assert len(batches) > 0
    for batch in batches:
        assert isinstance(batch, pa.RecordBatch)


def test_read_is_a_genuinely_lazy_iterator(connector_factory: ConnectorFactory) -> None:
    """Đây là bài quan trọng nhất trong bộ này, vì nó canh đúng thứ chữ ký
    `Iterator` (thay vì `Table`/`list`) tồn tại để ngăn.

    `iter([...])` sau khi đã gom hết bảng vào một list VẪN qua được bài
    `test_read_yields_record_batches` ở trên — nó cũng sinh ra `RecordBatch`.
    Cái nó không qua được là bài này: nếu `read()` gom hết trước rồi mới
    trả iterator, thì TOÀN BỘ chi phí (quét bảng, cấp phát RAM) đã xảy ra
    trước khi vòng lặp của người gọi lấy được phần tử đầu tiên. Một bảng lớn
    hơn RAM của pod sẽ OOMKill ngay ở dòng gọi `read()`, trước khi có cơ hội
    xử lý dù chỉ một batch.

    Cách bắt: chỉ LẤY MỘT batch rồi dừng — không gọi `list()`. Một cài đặt
    "gom trước, phát sau" vẫn phải làm xong toàn bộ việc gom trước khi trả về
    batch đầu tiên; với `n_rows` đủ lớn và `batch_size` nhỏ, phần khác biệt
    lộ ra ở việc CÓ sinh được batch đầu ngay hay không, không lộ ra ở thời
    gian (không đo thời gian ở đây vì thời gian không ổn định trên CI).
    Kiểm thêm `isinstance(..., Iterator)` để chặn luôn trường hợp trả về
    `list`/`tuple` — cả hai đều lặp được (iterable) nhưng không phải iterator,
    và Iterable thì `for` vẫn chạy dù nó là cả bảng nằm sẵn trong RAM.
    """
    connector = connector_factory(_DEFAULT_ROWS)
    stream = connector.discover()[0]
    result = connector.read(stream.name, StreamState())
    assert isinstance(result, Iterator)

    first_batch = next(result)
    assert isinstance(first_batch, pa.RecordBatch)
    assert first_batch.num_rows > 0
    # Không rút cạn `result`: mục đích của bài là chứng minh phần tử đầu tới
    # được MÀ KHÔNG CẦN đọc hết. Rút cạn ở đây sẽ không sai lệch kết quả bài
    # test, nhưng làm mất đi phần "chỉ lấy một rồi dừng" mà docstring mô tả.


def test_cursor_filter_is_inclusive_not_exclusive(connector_factory: ConnectorFactory) -> None:
    """Đọc gia tăng lọc theo `cursor_value` là `>=`, KHÔNG phải `>` — dòng có
    giá trị cursor ĐÚNG BẰNG mốc watermark BẮT BUỘC phải xuất hiện lại.

    Vì sao bắt buộc chứ không phải nên: `>` mất dữ liệu khi nhiều dòng chia sẻ
    cùng giá trị cursor (cùng `updated_at`, khác thời điểm commit) — dòng
    commit sau watermark được ghi nhận nhưng có cùng giá trị cursor với dòng
    đã đọc sẽ không bao giờ được đọc lại. Mất dữ liệu kiểu này KHÔNG đếm được
    và KHÔNG sửa được ở tầng silver, vì nó không để lại dấu vết nào để biết
    thiếu. Ngược lại `>=` sinh trùng lặp — trùng lặp thì ĐẾM ĐƯỢC và loại bỏ
    được ở silver (dedup theo khoá). Giữa "mất, im lặng" và "trùng, xử lý
    được", hợp đồng chọn trùng.

    Cách bắt: đọc hết một lần để có toàn bộ dữ liệu, chọn giá trị cursor ở
    một dòng GIỮA tập kết quả (không phải đầu, không phải cuối, để chắc chắn
    có cả dòng "trước" và dòng "tại/sau" mốc), đọc lại từ mốc đó, và khẳng
    định CHÍNH dòng mang giá trị mốc đó có mặt trong kết quả lần hai.
    """
    connector = connector_factory(_DEFAULT_ROWS)
    stream = connector.discover()[0]
    cursor_column = stream.candidate_cursors[0]

    full_batches = list(connector.read(stream.name, StreamState()))
    full_table = pa.Table.from_batches(full_batches)
    cursor_values = full_table.column(cursor_column).to_pylist()
    assert len(cursor_values) >= 2, "cần ít nhất 2 dòng để có một mốc giữa"

    sorted_values = sorted(cursor_values)
    boundary = sorted_values[len(sorted_values) // 2]

    resume_state = StreamState(cursor_column=cursor_column, cursor_value=str(boundary))
    resumed_batches = list(connector.read(stream.name, resume_state))
    resumed_table = pa.Table.from_batches(resumed_batches)
    resumed_values = resumed_table.column(cursor_column).to_pylist()

    assert boundary in resumed_values, (
        f"dòng có {cursor_column}={boundary!r} (mốc watermark) bị thiếu sau khi "
        "resume — lọc đang là '>' chứ không phải '>=', nghĩa là dữ liệu mất thật"
    )


def test_reading_an_unknown_stream_raises(connector_factory: ConnectorFactory) -> None:
    """Đọc một stream không tồn tại phải ném lỗi ngay, không được trả về
    iterator rỗng lặng lẽ — im lặng ở đây nguỵ trang một lỗi cấu hình
    (tên stream gõ sai) thành "đồng bộ xong, 0 dòng mới", điều mà người vận
    hành sẽ đọc nhầm thành thành công."""
    connector = connector_factory(_DEFAULT_ROWS)
    with pytest.raises(Exception):  # noqa: B017 - hợp đồng chỉ đòi "ném gì đó"
        list(connector.read("stream-khong-ton-tai", StreamState()))
