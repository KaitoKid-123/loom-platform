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


def _snapshots(lakekeeper: str, warehouse_name: str, s3_endpoint: str, qualified: str) -> int:
    """Số snapshot mà CATALOG thật sự đang lưu cho `qualified`.

    Catalog RIÊNG, tải lại bảng: commit cập nhật metadata trên đối tượng `Table`
    tại chỗ, nên con số đáng tin là con số catalog đã lưu chứ không phải bộ nhớ
    cục bộ của một client đã giữ bảng từ trước (cùng lập luận với
    `snapshot_count` trong `scripts/probe_iceberg_add_files.py`).
    """
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    return len(catalog.load_table(qualified).snapshots())


def test_create_empty_makes_a_table_with_no_rows_and_no_snapshot(
    lakehouse: Lakehouse, ns: str, lakekeeper: str, warehouse_name: str, s3_endpoint: str
) -> None:
    """`create_empty` cho ra một bảng CÓ schema, KHÔNG dòng, KHÔNG snapshot.

    Ba vế, ba lý do:

    * có schema — `register_files` sau đó nối cột theo tên, nên schema phải đúng
      TRƯỚC file đầu tiên;
    * không dòng — đây là chỗ `create_from` khác nó, và cả lý do nó tồn tại;
    * không snapshot — điều kiện để `test_register_files_lands_n_files_in_one_
      snapshot` đếm được "một snapshot" mà không phải trừ đi một snapshot khởi tạo.
    """
    qualified = f"{ns}.empty1"
    schema = pa.schema([pa.field("i", pa.int64()), pa.field("s", pa.string())])

    lakehouse.create_empty(qualified, schema)

    assert lakehouse.exists(qualified) is True
    assert lakehouse.schema(qualified).names == ["i", "s"]
    assert lakehouse.scan(qualified).read_all().num_rows == 0
    assert _snapshots(lakekeeper, warehouse_name, s3_endpoint, qualified) == 0


def test_register_files_lands_n_files_in_one_snapshot(
    lakehouse: Lakehouse, ns: str, lakekeeper: str, warehouse_name: str, s3_endpoint: str
) -> None:
    """BA file Parquet -> ĐÚNG một snapshot, và đọc lại đủ dòng.

    Đây là tính chất mà cả Giai đoạn 3d dựa lên: `scripts/probe_iceberg_add_files.py`
    đo nó ở N = 1, 5, 20 trên cụm thật, và bài này khoá nó lại qua CHÍNH hai hàm mà
    `IcebergSink` gọi. Một `register_files` gọi `add_files` cho từng file trong một
    vòng lặp vẫn cho đúng số dòng — chỉ phép đếm snapshot bắt được nó, và số commit
    catalog chính là thứ đang được cắt.

    Đọc lại dòng cũng là một phép canh chứ không trang trí: một snapshot không đăng
    ký được dòng nào cũng là "một snapshot".
    """
    qualified = f"{ns}.reg1"
    data = pa.table({"i": pa.array([1, 2], type=pa.int64())})
    lakehouse.create_empty(qualified, data.schema)

    writer = lakehouse.data_file_writer(qualified)
    uris = [
        writer.write(pa.table({"i": pa.array([n, n + 1], type=pa.int64())}), name=f"p{n}.parquet")
        for n in (1, 3, 5)
    ]
    lakehouse.register_files(qualified, uris)

    assert _snapshots(lakekeeper, warehouse_name, s3_endpoint, qualified) == 1
    landed = lakehouse.scan(qualified).read_all().column("i").to_pylist()
    assert sorted(landed) == [1, 2, 3, 4, 5, 6]


def test_the_written_file_lands_inside_the_tables_own_location(
    lakehouse: Lakehouse, ns: str
) -> None:
    """File phải nằm TRONG location của chính bảng — ràng buộc, không sở thích.

    Thăm dò Q4b đã đo trên Lakekeeper thật: hai vị trí khác (trong warehouse nhưng
    ngoài bảng, và ngoài warehouse) đều KHÔNG đăng ký được, vì credential STS mà
    Lakekeeper vend hẹp theo TỪNG BẢNG. Bài này canh rằng `DataFileWriter` dựng
    đường dẫn từ `table.location()` chứ không từ một tiền tố nào khác — nếu ai đó
    "dọn dẹp" nó thành một thư mục dùng chung, `register_files` sẽ ném
    `ACCESS_DENIED`, một câu không nhắc gì tới location.
    """
    qualified = f"{ns}.loc1"
    data = pa.table({"i": pa.array([1], type=pa.int64())})
    lakehouse.create_empty(qualified, data.schema)

    uri = lakehouse.data_file_writer(qualified).write(data, name="inside.parquet")

    assert uri.startswith("s3://")
    assert uri.endswith("/data/inside.parquet")
    lakehouse.register_files(qualified, [uri])
    assert lakehouse.scan(qualified).read_all().num_rows == 1


def test_registering_the_same_file_twice_is_refused_not_doubled(
    lakehouse: Lakehouse, ns: str
) -> None:
    """`check_duplicate_files` phải chặn — nếu không, bảng nhân đôi trong IM LẶNG.

    Thăm dò Q4c đã đo đúng điều đó với cờ TẮT: đăng ký lại một file đưa bảng từ
    1000 lên 2000 dòng, không lỗi, không dấu vết. Bài này là phép canh cho dòng
    `check_duplicate_files=True` trong `register_files` — tắt nó đi thì bài này đỏ
    ở khẳng định cuối (2 dòng thay vì 1) chứ không ở `pytest.raises`.
    """
    qualified = f"{ns}.dup1"
    data = pa.table({"i": pa.array([7], type=pa.int64())})
    lakehouse.create_empty(qualified, data.schema)
    uri = lakehouse.data_file_writer(qualified).write(data, name="once.parquet")
    lakehouse.register_files(qualified, [uri])

    with pytest.raises(ValueError, match="already referenced"):
        lakehouse.register_files(qualified, [uri])

    assert lakehouse.scan(qualified).read_all().num_rows == 1
