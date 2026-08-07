"""Sáu phép trên `Lakehouse`, chạy trên Lakekeeper THẬT.

`test_new_table_shows_up_in_the_listing` là phép DUY NHẤT bù cho một
`list_tables` bị hỏng theo kiểu cụ thể — xem docstring của nó.
"""

import uuid

import pyarrow as pa
import pytest

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
