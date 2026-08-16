"""`incremental` trên MỘT lakehouse thật và MỘT Postgres thật, không double nào ở giữa.

Bộ này KHÔNG tồn tại trước Giai đoạn 3d, và việc nó phải tồn tại bây giờ là hệ quả
trực tiếp của thay đổi 3d. Cho tới 3a, `IcebergSink.append` ghi VÀ commit trong một
lời gọi, nên `Lakehouse.append` (đã có test integration riêng ở `icebergkit`) là
toàn bộ phần Iceberg của đường `incremental`. Từ 3d, đường đó đi qua BA phép mới —
`create_empty`, `DataFileWriter.write`, `register_files` — và ba câu sau chỉ trả lời
được ở đây, với Lakekeeper thật:

1. Một file Parquet do `pyarrow` THUẦN ghi (không có field ID của Iceberg) có thật
   sự đọc lại ra ĐÚNG dữ liệu sau khi `add_files` đăng ký nó không. Thăm dò
   `scripts/probe_iceberg_add_files.py` (Q4a) đo rằng PyIceberg tự ghi
   `schema.name-mapping.default`, nhưng nó đo trên một schema HAI CỘT tự dựng —
   không phải trên schema THẬT của một bảng Postgres đi qua `add_bronze_columns`
   (`numeric` về string, `timestamptz`, cột nullable, `_ingested_at` có múi giờ).
2. Số SNAPSHOT có đúng bằng số NHÓM không. Đây là con số mà cả Giai đoạn 3d tồn
   tại để cắt, và không một bài unit nào đếm được nó — `RecordingSink` chỉ biết
   `commit()` đã được GỌI.
3. Nạp lại sau một cú đứt GIỮA nhóm có mất dòng không, khi những file Parquet của
   nhóm dở đã nằm trên S3 nhưng chưa được đăng ký.

Không dùng `main.ingest`, cùng lý do đã ghi ở `test_full_ingest.py`: nó dựng
`PostgresConnector` từ một `IngestSpec` cộng một Secret của pod, tức là kéo cả
`SourceCredentials`/`LakehouseSettings` (biến môi trường) vào một bộ test không
kiểm cấu hình. Việc `WriteTuning` tới được `run_incremental` được canh riêng ở
`tests/test_write_tuning.py`.
"""

from __future__ import annotations

import uuid

import pytest

from loom_connector import StreamState
from loom_connector.postgres import PostgresConnector
from loom_core.cursor import moves_forward
from loom_iceberg import Lakehouse, build_catalog
from loom_task.runner import (
    BRONZE_COLUMNS,
    Boom,
    bronze_table_name,
    resolve_cursor,
    run_incremental,
)
from loom_task.sink import IcebergSink

pytestmark = pytest.mark.integration

# Cùng slug (có dấu gạch NGANG) và cùng connection id với `test_full_ingest.py` —
# hai bộ nói về cùng một đường nạp, nên chúng không được dùng hai quy ước tên.
_CONNECTION_SLUG = "pos-aiven"
_CONNECTION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

# NĂM lô (500 dòng / 100 dòng mỗi lô, xem fixture `seeded_source`) với K = 2 cho
# hai nhóm ĐỦ và một nhóm DỞ. Đó là hình dạng nhỏ nhất phân biệt được một bản cài
# đặt quên commit nhóm cuối — ở cỡ thật (50 lô, K = 20) nhóm dở là 10 lô.
_COMMIT_EVERY = 2
_EXPECTED_SNAPSHOTS = 3

# Đứt ở lô thứ BA, tức GIỮA nhóm thứ hai: nhóm đầu đã commit và đã báo watermark,
# lô thứ ba đã ghi ra file Parquet mà chưa nhóm nào đăng ký nó. Một cú đứt ở lô 2
# hay lô 4 (đúng ranh giới nhóm) không đi qua trạng thái đó.
_CRASH_AT_BATCH = 3


class _Client:
    """Watermark sống ở CONTROL PLANE, nên nó sống ở đây — không ở pod.

    Giữ nguyên watermark giữa hai lần chạy là điều kiện để bài nạp lại có nghĩa:
    một client mới cho lần hai mô phỏng một thế giới nơi watermark biến mất cùng
    pod, và bài test khi đó chỉ chứng minh rằng đọc lại từ đầu thì không thiếu gì.

    Gọi CHÍNH `moves_forward` mà `loom-api._advance_watermark` gọi, không viết lại
    luật: so watermark bằng chuỗi thì `"199" > "99"` là `False` và mọi lần tiến hợp
    lệ sau lô đầu bị từ chối — bài test khi đó canh một hành vi không ai có (cùng
    lập luận đã ghi ở `tests/doubles.py`).

    Không dùng `RecordingClient` ở `tests/doubles.py`: thư mục này không phải một
    package (xem docstring conftest), nên `from doubles import ...` ở đây chỉ chạy
    khi pytest tình cờ đã chèn `tests/` vào `sys.path`.
    """

    def __init__(self) -> None:
        self.cursor_column: str | None = None
        self.cursor_value: str | None = None
        self.progress_calls: list[tuple[str | None, str | None, str | None, int]] = []

    @property
    def source_id(self) -> str:
        return str(_CONNECTION_ID)

    def current_state(self) -> StreamState:
        if self.cursor_column is None or self.cursor_value is None:
            return StreamState()
        return StreamState(cursor_column=self.cursor_column, cursor_value=self.cursor_value)

    def report_progress(
        self,
        *,
        rows: int,
        cursor_column: str | None = None,
        cursor_type: str | None = None,
        cursor_value: str | None = None,
    ) -> None:
        self.progress_calls.append((cursor_column, cursor_type, cursor_value, rows))
        if cursor_column is None or cursor_type is None or cursor_value is None:
            raise AssertionError("`incremental` PHẢI báo cả ba trường cursor — xem spec mục 4")
        if self.cursor_value is None or moves_forward(cursor_type, self.cursor_value, cursor_value):
            self.cursor_column = cursor_column
            self.cursor_value = cursor_value

    def complete(self, **kwargs: object) -> None:
        raise AssertionError("`complete` là việc của main.run_reporting_the_outcome")


def _target(stream: str) -> str:
    """Gọi CHÍNH `bronze_table_name` thay vì viết cứng — xem `test_full_ingest._target`."""
    return bronze_table_name(_CONNECTION_SLUG, stream)


def _ingest_incremental(
    lakehouse: Lakehouse,
    source: tuple[str, str, int, int],
    client: _Client,
    *,
    commit_every: int = _COMMIT_EVERY,
    crash_after_batch: int | None = None,
) -> int:
    """Một lần nạp `incremental`, đúng như `main.ingest` dựng nó — `run_id` MỚI mỗi lần.

    `run_id` mới cho mỗi lời gọi là mô phỏng đúng thực tế (mỗi lần bấm Nạp tạo một
    hàng `ingest_run` mới), và nó cũng là thứ làm phép canh tên file có nghĩa: hai
    lần chạy ghi vào CÙNG location của bảng đích, nên nếu tên file không mang
    `run_id` thì lần hai ghi đè lên file mà lần một đã đăng ký.
    """
    dsn, stream, _, batch_rows = source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)
    sink = IcebergSink(lakehouse, target=_target(stream), run_id=uuid.uuid4(), stream=stream)
    cursor = resolve_cursor(connector.discover(), stream, client.cursor_column)
    return run_incremental(connector, sink, client, stream, cursor, commit_every, crash_after_batch)


def _rows(lakehouse: Lakehouse, qualified: str) -> list[dict[str, object]]:
    return lakehouse.scan(qualified).read_all().to_pylist()


def _snapshots(lakekeeper: str, warehouse_name: str, s3_endpoint: str, qualified: str) -> int:
    """Số snapshot mà CATALOG đang lưu — tải bảng lại qua một catalog riêng.

    Không hỏi `Lakehouse`: nó cố ý chỉ nói Arrow ra ngoài và không phơi snapshot.
    Đây là một phép đo trong test, không phải một nhu cầu của mã sản phẩm.
    """
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    return len(catalog.load_table(qualified).snapshots())


def test_the_very_first_incremental_load_creates_the_target_and_lands_every_row(
    lakehouse: Lakehouse,
    seeded_source: tuple[str, str, int, int],
    lakekeeper: str,
    warehouse_name: str,
    s3_endpoint: str,
) -> None:
    """Lần nạp ĐẦU TIÊN của một stream: chưa có bảng bronze, và mọi dòng phải tới.

    Đây là bài trả lời câu 1 và câu 2 của docstring đầu file cùng một lúc:

    * `create_empty` dựng bảng từ schema Arrow THẬT (bốn cột Postgres +
      `add_bronze_columns`), rồi ba file Parquet pyarrow thuần được đăng ký và đọc
      lại ra đủ 500 dòng với đúng giá trị — nếu name-mapping không hoạt động thì
      các cột đọc ra TOÀN NULL (đúng hình dạng mà thăm dò Q4d thấy khi thiếu cột),
      nên phép so `amount` là phép canh thật chứ không trang trí.
    * Số snapshot bằng số NHÓM (3 cho 5 lô ở K = 2), không bằng số LÔ. Một bản cài
      đặt commit từng lô cho ra 5 và mọi khẳng định khác vẫn xanh.
    """
    _, stream, rows, batch_rows = seeded_source
    assert not lakehouse.exists(_target(stream))
    client = _Client()

    assert _ingest_incremental(lakehouse, seeded_source, client) == rows

    landed = _rows(lakehouse, _target(stream))
    assert len(landed) == rows
    assert sorted(int(row["id"]) for row in landed) == list(range(rows))
    assert set(BRONZE_COLUMNS) <= set(landed[0])
    assert {row["_source"] for row in landed} == {str(_CONNECTION_ID)}
    # `numeric` của Postgres về Arrow string (xem `PostgresConnector`), và một
    # `add_files` không nối được cột sẽ trả về None ở đây.
    assert {row["amount"] for row in landed if int(row["id"]) == 0} == {"19.99"}

    assert (
        _snapshots(lakekeeper, warehouse_name, s3_endpoint, _target(stream))
        == _EXPECTED_SNAPSHOTS
        != rows // batch_rows
    )


def test_one_watermark_is_reported_per_group_not_per_batch(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """MỘT lời báo cho mỗi nhóm, mang mốc của lô CUỐI và số dòng của CẢ nhóm.

    Vế "số dòng của cả nhóm" là vế dễ sai và im lặng nhất: `ingest_run.rows_written`
    chỉ cộng dồn qua `/progress`, nên báo `batch.num_rows` cho một nhóm K lô làm cột
    đó thiếu đúng K lần và không có gì trong hệ thống mâu thuẫn với nó.

    Cũng là chỗ chứng minh "báo watermark thưa hơn" của backlog tự đạt được: 5 lô
    cho 3 lời báo, không 5 — cùng một thay đổi, không phải hai việc.
    """
    _, _, rows, batch_rows = seeded_source
    client = _Client()

    _ingest_incremental(lakehouse, seeded_source, client)

    assert client.progress_calls == [
        ("id", "integer", "199", 2 * batch_rows),
        ("id", "integer", "399", 2 * batch_rows),
        ("id", "integer", "499", batch_rows),
    ]
    assert sum(call[3] for call in client.progress_calls) == rows
    assert client.cursor_value == "499"


def test_a_crash_between_two_groups_loses_no_rows(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Chạy, giết GIỮA một nhóm, chạy lại — TẬP HỢP dòng phải đầy đủ.

    Đây là hợp đồng at-least-once của spec mục 4 trên đường Iceberg thật, và là
    phép canh cho thứ tự "commit rồi mới báo". Cú đứt ở lô 3 để lại đúng trạng thái
    đáng lo: một file Parquet của nhóm thứ hai ĐÃ nằm trên S3 mà chưa được đăng ký,
    nên nó vô hình với mọi người đọc — nếu watermark đã tiến qua nó thì 100 dòng đó
    mất, không lỗi, không dấu vết.

    Khẳng định TẬP HỢP chứ không số dòng: lọc cursor là `>=`, nên dòng mang đúng giá
    trị watermark quay lại ở lần chạy sau. Trùng thì được, thiếu thì không.
    """
    _, stream, rows, _ = seeded_source
    client = _Client()

    with pytest.raises(Boom):
        _ingest_incremental(lakehouse, seeded_source, client, crash_after_batch=_CRASH_AT_BATCH)
    # Nhóm đầu đã bền, và watermark đứng ở đúng mốc của nó — không xa hơn.
    assert client.cursor_value == "199"
    assert len(_rows(lakehouse, _target(stream))) == 2 * seeded_source[3]

    _ingest_incremental(lakehouse, seeded_source, client)

    landed = _rows(lakehouse, _target(stream))
    assert {int(row["id"]) for row in landed} == set(range(rows)), "mất dòng"
    assert len(landed) > rows, "at-least-once: dòng ở đúng mốc watermark phải quay lại"


def test_two_runs_write_files_that_do_not_overwrite_each_other(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Hai lần nạp vào CÙNG bảng đích ghi vào CÙNG thư mục — và không được đụng nhau.

    `add_files` chỉ đăng ký được file nằm trong location của chính bảng (thăm dò
    Q4b), nên mọi lần chạy `incremental` của một stream dùng chung một thư mục
    `data/`. Với một cái tên chỉ mang số thứ tự lô, lần chạy thứ hai sẽ ghi ĐÈ lên
    file mà lần thứ nhất đã đăng ký — nội dung của những dòng đã nằm trong bảng đổi
    dưới chân người đọc, và `check_duplicate_files` không thấy gì vì tên file
    trùng nghĩa là nó KHÔNG được đăng ký lại.

    Bài này chạy hai lần nạp ĐẦY ĐỦ (lần hai nối thêm 1 dòng vì lọc `>=`) rồi đếm
    lại: 501 dòng, và mọi `id` của nguồn còn nguyên. Bỏ `run_id` khỏi tên file thì
    lần hai ghi đè file đầu của lần một và số dòng đọc lại được sẽ khác.
    """
    _, stream, rows, _ = seeded_source
    client = _Client()

    _ingest_incremental(lakehouse, seeded_source, client)
    _ingest_incremental(lakehouse, seeded_source, client)

    landed = _rows(lakehouse, _target(stream))
    assert {int(row["id"]) for row in landed} == set(range(rows))
    assert len(landed) == rows + 1, "lần hai chỉ nên nối thêm dòng ở đúng mốc watermark"
