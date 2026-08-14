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
lakehouse thật — còn phần chọn mode của `main.ingest` được canh ở
`tests/test_main_ingest_mode.py`.

Câu cuối đó KHÔNG đúng khi nó được viết ra (Task 12): không có bài test nào cho
nhánh chọn mode cho tới Task 15, và một chú thích khẳng định "đã có test riêng"
là cách chắc chắn nhất để không ai đi viết nó. Ghi lại ở đây thay vì lặng lẽ sửa
— chính lớp sai này là thứ repo canh.
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

# "Connection" mà mọi bài dưới đây nạp qua. Cố định là đủ: mỗi bài có warehouse
# riêng (xem fixture `warehouse_name`), nên hai bài không nhìn thấy bảng của nhau
# dù tên bảng đích giống nhau.
#
# Slug mang một dấu GẠCH NGANG có chủ đích — đó là khuôn mà `ItemCreate.name` cho
# phép và là khuôn mà fixture của repo dùng (`can-sua`). Nó bắt bộ integration
# này đi qua đúng đường mã hoá `-` -> `_` mà bảng bronze cần để truy vấn được, thay
# vì một slug "sạch" tình cờ không cần mã hoá gì.
_CONNECTION_SLUG = "pos-aiven"
_CONNECTION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

# Lô thứ 2 trong NĂM lô — đứt ở GIỮA. Một crash sau lô cuối không phân biệt được
# với một lần chạy xong, nên nó không kiểm được điều bài crash tồn tại để kiểm.
_CRASH_AT_BATCH = 2


class _Client:
    """Đúng phần `IngestClientLike` mà `run_full` gọi tới, không hơn.

    `run_full` đọc `source_id` (giá trị cột bronze `_source`) và báo số dòng, nhưng
    KHÔNG được gọi `current_state()` — `full` đọc lại từ đầu (spec mục 5). Nên
    phương thức đó ném ở đây: một `full` đọc watermark sẽ lặng lẽ trở thành
    `incremental` rồi THAY cả bảng bằng phần đuôi vừa đọc, và một `AssertionError`
    đọc được rẻ hơn nhiều so với việc đi tìm những dòng đã biến mất.

    Không dùng `RecordingClient` ở `tests/doubles.py`: thư mục này không phải một
    package (xem docstring conftest), nên `from doubles import ...` ở đây chỉ chạy
    khi pytest tình cờ đã chèn `tests/` vào `sys.path` vì đang collect cả bộ unit
    test — tức là bộ này sẽ hỏng khi có ai chạy riêng nó.
    """

    def __init__(self) -> None:
        self.rows_reported: list[int] = []

    @property
    def source_id(self) -> str:
        return str(_CONNECTION_ID)

    def current_state(self) -> StreamState:
        raise AssertionError("`full` đọc lại từ đầu — nó không được hỏi watermark")

    def report_progress(
        self,
        *,
        rows: int,
        cursor_column: str | None = None,
        cursor_type: str | None = None,
        cursor_value: str | None = None,
    ) -> None:
        """`full` báo SỐ DÒNG nhưng không bao giờ báo cursor — xem spec mục 5.

        Ném khi có cursor chứ không im lặng bỏ qua: một `full` đẩy watermark lên
        làm lần `incremental` KẾ TIẾP bỏ qua đúng khoảng dữ liệu vừa đọc, và hậu
        quả xuất hiện ở một lần chạy khác nên không ai nối được với nguyên nhân.
        Bài đơn vị canh cùng tính chất trên chuỗi lời gọi; ở đây nó là một cái
        chốt cho đường đi qua Iceberg thật.
        """
        if (cursor_column, cursor_type, cursor_value) != (None, None, None):
            raise AssertionError(
                "`full` không đụng watermark — xem spec mục 5; nhận "
                f"{(cursor_column, cursor_type, cursor_value)!r}"
            )
        self.rows_reported.append(rows)

    def complete(self, **kwargs: object) -> None:
        raise AssertionError("`complete` là việc của main.run_reporting_the_outcome")


def _target(stream: str) -> str:
    """Tên bảng bronze mà `main.target_table` sẽ tính ra cho spec này.

    Gọi CHÍNH `bronze_table_name` thay vì viết cứng một chuỗi: một tên viết cứng ở
    đây và một tên tính ở mã sản phẩm là hai thứ phải giữ khớp nhau bằng trí nhớ,
    và bài `test_the_swap_leaves_no_extra_table_in_the_namespace` (so danh sách
    bảng THẬT) sẽ đỏ với một thông báo về `list_tables` khi thứ sai thật là quy
    ước tên.
    """
    return bronze_table_name(_CONNECTION_SLUG, stream)


def _ingest_full(
    lakehouse: Lakehouse,
    source: tuple[str, str, int, int],
    *,
    run_id: uuid.UUID | None = None,
    crash_after_batch: int | None = None,
    client: _Client | None = None,
) -> int:
    """Một lần nạp `full`, đúng như `main.ingest` dựng nó — `run_id` MỚI mỗi lần.

    `run_id` mới cho mỗi lần gọi là mô phỏng đúng thực tế: mỗi lần bấm Nạp tạo
    một hàng `ingest_run` mới, và tên bảng staging/`đích_cũ` bám theo `run_id` đó
    (xem `loom_task.sink`).
    """
    dsn, stream, _, batch_rows = source
    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)
    sink = IcebergSink(
        lakehouse, target=_target(stream), run_id=run_id or uuid.uuid4(), stream=stream
    )
    return run_full(connector, sink, client or _Client(), stream, crash_after_batch)


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


def test_full_reports_every_staged_batch_so_the_row_count_is_not_zero(
    lakehouse: Lakehouse, seeded_source: tuple[str, str, int, int]
) -> None:
    """`ingest_run.rows_written` phải có một con số THẬT cho một run `full`.

    `/progress` là đường DUY NHẤT cộng dồn cột đó (`/complete` cố ý không mang
    `rows` — xem `IngestCompletionReport`), nên một `run_full` không báo gì để nó
    ở 0 suốt cả lần nạp: giao diện 3c hiển thị "đã nạp 0 dòng" cho một lần nạp
    500 dòng đã xong. Bài này khẳng định tổng báo về khớp số dòng nguồn VÀ khớp
    số dòng thật sự hạ cánh trong bảng đích — hai con số đó bằng nhau khi cú tráo
    làm đúng việc.

    `_Client.report_progress` ném nếu có bất kỳ trường cursor nào, nên bài này
    cũng là cái chốt "không đụng watermark" cho đường đi qua Iceberg thật.
    """
    _, stream, rows, batch_rows = seeded_source
    client = _Client()

    _ingest_full(lakehouse, seeded_source, client=client)

    assert sum(client.rows_reported) == rows
    assert client.rows_reported == [batch_rows] * (rows // batch_rows)
    assert len(_read_all(lakehouse, _target(stream))) == sum(client.rows_reported)


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
