"""Các phép trên `Lakehouse`, chạy trên Lakekeeper THẬT.

`test_new_table_shows_up_in_the_listing` là phép DUY NHẤT bù cho một
`list_tables` bị hỏng theo kiểu cụ thể — xem docstring của nó.
"""

import uuid

import pyarrow as pa
import pytest
from pyiceberg.exceptions import TableAlreadyExistsError

from loom_iceberg import Lakehouse, build_catalog

pytestmark = pytest.mark.integration


@pytest.fixture
def ns(lakehouse: Lakehouse) -> str:
    name = f"ns_{uuid.uuid4().hex[:8]}"
    lakehouse.create_namespace(name)
    return name


def test_create_from_then_read_back(lakehouse: Lakehouse, ns: str) -> None:
    qualified = f"{ns}.t1"
    data = pa.table({"i": pa.array([1, 2, 3], type=pa.int64()), "s": ["a", "b", "c"]})
    lakehouse.create_from(qualified, data)

    result = lakehouse.scan(qualified).read_all()
    assert result.num_rows == 3
    assert result.sort_by("i").to_pydict() == {"i": [1, 2, 3], "s": ["a", "b", "c"]}


def test_new_table_shows_up_in_the_listing(lakehouse: Lakehouse, ns: str) -> None:
    """Explorer ở Giai đoạn 2c đọc CHÍNH hàm này để vẽ danh sách bảng.

    KHÔNG phép nào khác trong file này gọi `list_tables`. Sửa nó thành
    `return []` rồi chạy lại cả file: phép này phải là phép DUY NHẤT đỏ, năm
    phép còn lại (đọc/viết/schema/exists/scan) không hề chạm đường này —
    đã tự tay kiểm trước khi commit.
    """
    qualified = f"{ns}.t2"
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1], type=pa.int64())}))

    tables = lakehouse.list_tables(ns)
    assert [t.qualified for t in tables] == [qualified]


def test_schema_reports_column_names_and_types(lakehouse: Lakehouse, ns: str) -> None:
    """Nguồn autocomplete ở Giai đoạn 2c — kiểm cả TÊN lẫn KIỂU, không chỉ một vế."""
    qualified = f"{ns}.t3"
    data = pa.table(
        {
            "i": pa.array([1], type=pa.int64()),
            "s": pa.array(["a"], type=pa.string()),
            "d": pa.array([1.5], type=pa.float64()),
        }
    )
    lakehouse.create_from(qualified, data)

    schema = lakehouse.schema(qualified)
    assert schema.names == ["i", "s", "d"]
    assert schema.field("i").type == pa.int64()
    # Iceberg "string" luôn ánh xạ về `large_string` khi đi qua PyIceberg,
    # bất kể kiểu Arrow gốc lúc tạo bảng là `string` hay `large_string`.
    assert schema.field("s").type == pa.large_string()
    assert schema.field("d").type == pa.float64()


def test_append_adds_rows_without_replacing(lakehouse: Lakehouse, ns: str) -> None:
    qualified = f"{ns}.t4"
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1], type=pa.int64())}))
    lakehouse.append(qualified, pa.table({"i": pa.array([2, 3], type=pa.int64())}))

    result = lakehouse.scan(qualified).read_all()
    assert sorted(result.column("i").to_pylist()) == [1, 2, 3]


def test_exists_is_false_before_and_true_after(lakehouse: Lakehouse, ns: str) -> None:
    qualified = f"{ns}.t5"
    assert lakehouse.exists(qualified) is False
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1], type=pa.int64())}))
    assert lakehouse.exists(qualified) is True


def test_scan_returns_a_streaming_reader_not_a_materialised_table(
    lakehouse: Lakehouse, ns: str
) -> None:
    """`RecordBatchReader`, không `pa.Table` — xem docstring của `Lakehouse.scan`."""
    qualified = f"{ns}.t6"
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1], type=pa.int64())}))

    reader = lakehouse.scan(qualified)
    assert isinstance(reader, pa.RecordBatchReader)


def test_create_namespace_if_not_exists_is_a_noop_the_second_time(
    lakehouse: Lakehouse, ns: str
) -> None:
    """`ns` (fixture) đã tạo namespace một lần qua `create_namespace` — gọi lại
    qua bản KHÔNG-ném-lỗi này không được ném gì, khác `create_namespace` thô
    (đã ném `NamespaceAlreadyExistsError` nếu gọi lại — hành vi CŨ, không đổi).

    Runner (Giai đoạn 2c, CTAS) cần bản này: nó không biết trước một namespace
    đích đã tồn tại hay chưa, và hỏi trước bằng `list_namespaces()` rồi mới gọi
    `create_namespace` là hai round trip cho đúng một câu hỏi mà PyIceberg trả
    lời được trong một."""
    lakehouse.create_namespace_if_not_exists(ns)  # không ném, dù `ns` đã tồn tại
    assert ns in lakehouse.list_namespaces()


def test_create_from_with_replace_overwrites_an_existing_table(
    lakehouse: Lakehouse, ns: str
) -> None:
    """`CREATE OR REPLACE TABLE ... AS SELECT` (Giai đoạn 2c): đích ĐÃ tồn
    tại, và dữ liệu CŨ phải biến mất — không phải `append`, cũng không phải
    một lỗi "đã tồn tại"."""
    qualified = f"{ns}.t8"
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1, 2, 3], type=pa.int64())}))

    lakehouse.create_from(qualified, pa.table({"i": pa.array([9], type=pa.int64())}), replace=True)

    result = lakehouse.scan(qualified).read_all()
    assert result.column("i").to_pylist() == [9]


def test_create_from_without_replace_still_rejects_an_existing_table(
    lakehouse: Lakehouse, ns: str
) -> None:
    """Vế KHẲNG ĐỊNH của bài trên: hành vi MẶC ĐỊNH (`replace=False`, hành vi
    CŨ) vẫn phải từ chối ghi đè một bảng đã tồn tại — không có bài này, một
    cài đặt lỡ để `replace=True` làm mặc định cũng làm bài trên xanh mà không
    chứng minh được gì về tham số `replace`."""
    qualified = f"{ns}.t9"
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1], type=pa.int64())}))

    with pytest.raises(Exception):  # noqa: B017 — lỗi thật từ PyIceberg, không đoán loại
        lakehouse.create_from(qualified, pa.table({"i": pa.array([2], type=pa.int64())}))


def test_rename_table_moves_the_data_and_retires_the_old_name(
    lakehouse: Lakehouse, ns: str
) -> None:
    """`rename_table` là MOVE, không COPY — và cả Task 12 dựa vào điều đó.

    Chuỗi tráo bảng của `mode: full` (`loom_task.run_full`) đọc `rename` là "dữ
    liệu đi theo cái tên": bước 2 gửi bảng đích đi chỗ khác NGUYÊN VẸN, bước 3
    đưa staging vào. Nếu `rename` là copy thì tên cũ còn lại và bước 3 hỏng vì
    đích vẫn tồn tại; nếu nó không giữ dữ liệu thì cú tráo là một cách xoá bảng
    có nhiều bước.

    ĐO 2 mục D đã đo cả hai tính chất này TRONG CỤM một lần (xem
    `scripts/probe_iceberg_single_commit.py`). Bài này giữ chúng lại trong bộ
    test chạy mỗi lần push, vì một script đo đã chạy một lần không phát hiện
    được ngày Lakekeeper đổi hành vi.
    """
    source = f"{ns}.t10"
    destination = f"{ns}.t10_renamed"
    lakehouse.create_from(source, pa.table({"i": pa.array([1, 2, 3], type=pa.int64())}))

    lakehouse.rename_table(source, destination)

    assert lakehouse.scan(destination).read_all().column("i").to_pylist() == [1, 2, 3]
    assert not lakehouse.exists(source), "rename phải là MOVE — tên cũ không được còn"


def test_rename_table_refuses_to_overwrite_a_name_that_already_exists(
    lakehouse: Lakehouse, ns: str
) -> None:
    """Vế phủ định, và nó là LÝ DO chuỗi tráo của `full` có ba bước.

    ĐO 2 mục D4 đã đo: đè lên một tên đang tồn tại bị TỪ CHỐI
    (`TableAlreadyExistsError`). Nếu điều này một ngày nào đó trở thành ĐƯỢC
    PHÉP, cú tráo của `full` gọn lại thành MỘT lời gọi nguyên tử thật, và bài
    này đỏ để nói ra điều đó thay vì để một chuỗi ba bước tồn tại mãi vì không
    ai kiểm lại giả định của nó.
    """
    target = f"{ns}.t11"
    staging = f"{ns}.t11_staging"
    lakehouse.create_from(target, pa.table({"i": pa.array([1], type=pa.int64())}))
    lakehouse.create_from(staging, pa.table({"i": pa.array([2], type=pa.int64())}))

    with pytest.raises(TableAlreadyExistsError):
        lakehouse.rename_table(staging, target)

    assert lakehouse.scan(target).read_all().column("i").to_pylist() == [1]


def test_scan_size_bytes_matches_pyiceberg_manifest_stats_and_two_files_sum(
    lakehouse: Lakehouse,
    ns: str,
    lakekeeper: str,
    s3_endpoint: str,
    warehouse_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Thăm dò + canh cho `Lakehouse.scan_size_bytes` (Task 8, loom-query).

    In nguyên văn thứ `Table.scan().plan_files()` phơi ra — xem docstring
    `scan_size_bytes` cho lý do đây là con số đúng để kiểm trần byte quét
    TRƯỚC khi đọc. `append` tạo data file THỨ HAI, nên phép cộng dồn (không
    phải chỉ đọc file đầu) thật sự bị kiểm.
    """
    qualified = f"{ns}.t7"
    lakehouse.create_from(qualified, pa.table({"i": pa.array([1, 2, 3], type=pa.int64())}))
    lakehouse.append(qualified, pa.table({"i": pa.array([4, 5], type=pa.int64())}))

    # Catalog RIÊNG cho thăm dò — cùng quy ước "mỗi query một catalog" mà
    # `runner.py` dùng, để không chia sẻ trạng thái với fixture `lakehouse`.
    probe_catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    table = probe_catalog.load_table(qualified)
    tasks = list(table.scan().plan_files())

    with capsys.disabled():
        print(f"\nplan_files() cho {qualified}: {len(tasks)} FileScanTask")
        for task in tasks:
            print(
                f"  file_path={task.file.file_path!r} "
                f"file_size_in_bytes={task.file.file_size_in_bytes} "
                f"record_count={task.file.record_count}"
            )

    assert len(tasks) == 2, "hai lần create_from/append phải cho ra hai data file riêng"
    expected_total = sum(task.file.file_size_in_bytes for task in tasks)
    assert all(task.file.file_size_in_bytes > 0 for task in tasks)

    assert lakehouse.scan_size_bytes(qualified) == expected_total
