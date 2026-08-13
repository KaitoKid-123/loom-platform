"""`full` trên MỘT lakehouse thật và MỘT Postgres thật, không double nào ở giữa.

Bài test đơn vị (`tests/test_runner_full.py`) canh THỨ TỰ các bước qua một sink
ghi lại sự kiện. Nó không biết — và không thể biết — rằng `rename_table` của
Lakekeeper thật giữ nguyên dữ liệu, rằng schema Arrow của một bảng Postgres thật
đi qua `create_from` được, hay rằng một bảng `..._old_<hex>` sót lại từ một lần
chạy chết không chặn lần chạy sau. Đó là ba câu chỉ trả lời được ở đây.

Không dùng `main.ingest`: nó dựng `PostgresConnector` từ một `IngestSpec` và một
Secret của pod, tức là kéo cả `SourceCredentials` + `LakehouseSettings` (biến môi
trường) vào bộ test này để kiểm một tính chất không dính gì tới cấu hình. Bộ này
gọi `run_full` với đúng ba thứ nó cần — connector thật, `IcebergSink` thật,
lakehouse thật — còn phần chọn mode của `main.ingest` đã có test riêng.
"""

from __future__ import annotations

import uuid

import pyarrow as pa
import pytest

from loom_connector import StreamState
from loom_connector.postgres import PostgresConnector
from loom_iceberg import Lakehouse
from loom_task.runner import BRONZE_COLUMNS, Boom, bronze_table_name, run_full
from loom_task.sink import IcebergSink, old_target_name, staging_table_name

pytestmark = pytest.mark.integration

# `connection_id` của "connection" mà mọi bài dưới đây nạp qua. Cố định là đủ:
# mỗi bài có warehouse riêng (xem fixture `warehouse_name`), nên hai bài không
# nhìn thấy bảng của nhau dù tên bảng đích giống nhau.
_CONNECTION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

# Lô thứ 2 trong NĂM lô — đứt ở GIỮA. Một crash sau lô cuối không phân biệt được
# với một lần chạy xong, nên nó không kiểm được điều bài crash tồn tại để kiểm.
_CRASH_AT_BATCH = 2


class _Client:
    """Đúng phần `IngestClientLike` mà `run_full` gọi tới, không hơn.

    `run_full` chỉ đọc `source_id` (giá trị cột bronze `_source`) và KHÔNG được
    gọi `current_state()` hay `report_progress()` — `full` không có watermark
    (spec mục 5). Nên hai phương thức đó ném ở đây: một `full` đọc watermark sẽ
    lặng lẽ trở thành `incremental` rồi THAY cả bảng bằng phần đuôi vừa đọc, và
    một `AssertionError` đọc được rẻ hơn nhiều so với việc đi tìm những dòng đã
    biến mất.

    Không dùng `RecordingClient` ở `tests/doubles.py`: thư mục này không phải một
    package (xem docstring conftest), nên `from doubles import ...` ở đây chỉ chạy
    khi pytest tình cờ đã chèn `tests/` vào `sys.path` vì đang collect cả bộ unit
    test — tức là bộ này sẽ hỏng khi có ai chạy riêng nó.
    """

    @property
    def source_id(self) -> str:
        return str(_CONNECTION_ID)

    def current_state(self) -> StreamState:
        raise AssertionError("`full` đọc lại từ đầu — nó không được hỏi watermark")

    def report_progress(self, **kwargs: object) -> None:
        raise AssertionError("`full` không đụng watermark — xem spec mục 5")

    def complete(self, **kwargs: object) -> None:
        raise AssertionError("`complete` là việc của main.run_reporting_the_outcome")


def _target(stream: str) -> str:
    """Tên bảng bronze mà `main._build_sink` sẽ tính ra cho spec này.

    `connection_id.hex` làm slug là khoảng trống đã biết của Task 12 (`IngestSpec`
    không mang tên connection) — dùng lại ĐÚNG cách tính đó ở đây thay vì một tên
    tự đặt, để bộ test này đi qua cùng một hàm với mã sản phẩm.
    """
    return bronze_table_name(_CONNECTION_ID.hex, stream)


def _ingest_full(
    lakehouse: Lakehouse,
    source: tuple[str, str, int, int],
    *,
    run_id: uuid.UUID | None = None,
    crash_after_batch: int | None = None,
) -> int:
    """Một lần nạp `full`, đúng như `main.ingest` dựng nó — `run_id` MỚI mỗi lần.

    `run_id` mới cho mỗi lần gọi là mô phỏng đúng thực tế: mỗi lần bấm Nạp tạo
    một hàng `ingest_run` mới, và tên bảng staging/`đích_cũ` bám theo `run_id` đó
    (xem `loom_task.sink`).
    """
    dsn, stream, _, batch_rows = source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)
    sink = IcebergSink(lakehouse, target=_target(stream), run_id=run_id or uuid.uuid4())
    return run_full(connector, sink, _Client(), stream, crash_after_batch)


def _read_all(lakehouse: Lakehouse, qualified: str) -> list[dict[str, object]]:
    """Mọi dòng, sắp theo `id` — so NỘI DUNG, không chỉ số dòng.

    Sắp xếp vì thứ tự dòng của một scan Iceberg không phải một bảo đảm (nó theo
    thứ tự data file), nên một phép so phụ thuộc thứ tự sẽ đỏ theo số lô chứ
    không theo lỗi thật. So cả `dict` chứ không chỉ `id` là điều kiện để bài
    "bảng cũ còn nguyên" có nghĩa: một bản cài đặt ghi thẳng vào đích rồi dọn có
    thể giữ đúng tập `id` mà thay hết `_batch_id`/`_ingested_at`.
    """
    table = lakehouse.scan(qualified).read_all()
    return sorted(table.to_pylist(), key=lambda row: row["id"])


def test_the_very_first_full_load_creates_the_target(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Lần nạp ĐẦU TIÊN: không có bảng đích, nên không có gì để đổi tên đi.

    Đường mà MỌI lakehouse mới đi qua đúng một lần. Bài đơn vị canh chuỗi sự kiện
    của nó; bài này canh thứ mà chuỗi sự kiện không nói được — rằng
    `rename(staging -> đích)` vào một cái tên CHƯA TỒN TẠI thật sự chạy qua
    Lakekeeper, và bảng hiện ra với đủ dòng lẫn đủ cột.
    """
    _, stream, rows, _ = seeded_source
    assert not lakehouse.exists(_target(stream))

    assert _ingest_full(lakehouse, seeded_source) == rows

    landed = _read_all(lakehouse, _target(stream))
    assert len(landed) == rows
    # Ba cột metadata phải đi VÀO staging: cú tráo chỉ đổi TÊN, nó không viết lại
    # dòng nào, nên một `run_full` quên `add_bronze_columns` không có bước nào
    # sau đó chữa được.
    assert set(BRONZE_COLUMNS) <= set(landed[0])
    assert {row["_source"] for row in landed} == {str(_CONNECTION_ID)}
    # `numeric` của Postgres về Arrow string (xem `PostgresConnector`) và đi qua
    # `create_from` được — kiểu này là chỗ một lỗi chuyển schema lộ ra.
    assert landed[0]["amount"] == "19.99"


def test_full_replaces_it_does_not_pile_up(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Nạp full hai lần cho ra ĐÚNG số dòng của nguồn, không gấp đôi.

    Đây là mục nghiệm thu trực tiếp của quyết định "full = đọc cả bảng RỒI THAY"
    mà chủ dự án chốt 2026-08-11. Một bản cài đặt append (hoặc một cú tráo không
    xảy ra) cho ra 1000 dòng và không lỗi gì.
    """
    _, stream, rows, _ = seeded_source
    _ingest_full(lakehouse, seeded_source)
    first = len(_read_all(lakehouse, _target(stream)))

    _ingest_full(lakehouse, seeded_source)

    assert len(_read_all(lakehouse, _target(stream))) == first == rows


def test_the_swap_leaves_no_extra_table_in_the_namespace(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Sau hai lần nạp XONG, `bronze` chỉ còn ĐÚNG bảng đích.

    Bước 4 (`drop(đích_cũ)`) là bước dễ bỏ nhất — bỏ nó thì mọi bài khác vẫn
    xanh, và mỗi lần nạp `full` để lại một bản sao đầy đủ của bảng cũ trong
    catalog. Không ai thấy cho tới khi hoá đơn S3 tới, hoặc tới khi có người mở
    danh sách bảng và thấy mười cái tên rác.
    """
    _, stream, _, _ = seeded_source
    first_run = uuid.uuid4()
    _ingest_full(lakehouse, seeded_source, run_id=first_run)
    _ingest_full(lakehouse, seeded_source)

    names = sorted(info.qualified for info in lakehouse.list_tables("bronze"))
    assert names == [_target(stream)], names
    assert staging_table_name(_target(stream), first_run) not in names
    assert old_target_name(_target(stream), first_run) not in names


def test_a_crash_during_full_leaves_the_old_table_intact(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Phép canh QUAN TRỌNG NHẤT của mode `full`.

    Thiếu nó thì "bảng đích không bị chạm tới" chỉ là một lời hứa trong tài liệu:
    mọi phép kiểm khác vẫn xanh với một bản cài đặt ghi thẳng vào bảng đích rồi
    dọn sau, cho tới ngày một lần nạp hỏng giữa chừng và xoá mất bảng của ai đó.

    So NỘI DUNG từng dòng, không chỉ số dòng: một bản ghi-thẳng-vào-đích đứt ở lô
    2/5 để lại 200 dòng MỚI (đúng `id`, khác `_batch_id`) thay cho 500 dòng cũ, và
    một phép so chỉ đếm dòng cũng bắt được — nhưng đổi `crash_after_batch` thành
    5 thì không, còn phép so nội dung thì vẫn.
    """
    _, stream, _, _ = seeded_source
    _ingest_full(lakehouse, seeded_source)
    before = _read_all(lakehouse, _target(stream))

    with pytest.raises(Boom):
        _ingest_full(lakehouse, seeded_source, crash_after_batch=_CRASH_AT_BATCH)

    assert _read_all(lakehouse, _target(stream)) == before


def test_a_leftover_old_target_from_a_dead_run_does_not_block_the_next_run(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """Rác của một lần chạy chết KHÔNG được chặn lần chạy sau, vĩnh viễn.

    ĐO 2 mục D4: `rename_table` TỪ CHỐI đè lên một tên đang tồn tại. Với một tên
    `đích_cũ` CỐ ĐỊNH, một lần chạy chết giữa bước 2 và bước 4 để lại cái tên đó,
    và lần chạy sau hỏng ở bước 2 với `TableAlreadyExistsError` — rồi mọi lần sau
    nữa cũng vậy, tới khi có người vào xoá tay. Bài này dựng lại đúng cảnh đó:
    một bảng mang tên `đích_cũ` CỦA MỘT RUN KHÁC đứng sẵn trong `bronze`.

    Nó đứng được vì tên mang hậu tố `run_id`. Bảng rác vẫn còn sau đó — không lần
    chạy nào nhận ra nó là rác, đó là cái giá đã ghi ở `loom_task.sink` — nhưng nó
    không chặn gì, và một lần nạp KHÔNG được tự ý xoá dữ liệu nó không tạo ra.
    """
    _, stream, rows, _ = seeded_source
    _ingest_full(lakehouse, seeded_source)
    leftover = old_target_name(_target(stream), uuid.uuid4())
    lakehouse.create_from(leftover, pa.table({"id": pa.array([1], type=pa.int64())}))

    assert _ingest_full(lakehouse, seeded_source) == rows

    assert len(_read_all(lakehouse, _target(stream))) == rows
    assert lakehouse.exists(leftover), "rác của run khác KHÔNG được lần nạp này xoá đi"
