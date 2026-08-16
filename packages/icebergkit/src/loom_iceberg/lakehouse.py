"""Đọc/viết bảng Iceberg qua một `RestCatalog`, chỉ nói Apache Arrow ra ngoài.

Mọi giá trị `Lakehouse` trả về là Arrow (`pa.Schema`, `pa.RecordBatchReader`),
không phải kiểu của PyIceberg (`Schema`, `Table`, `DataScan`...). Đó là điều
làm cho việc đổi engine ở spec v1 mục 5.9 khả thi thay vì chỉ là một lời hứa:
chỗ gọi `Lakehouse` không cần biết PyIceberg tồn tại.
"""

from collections.abc import Sequence
from dataclasses import dataclass

# pyarrow (25.0.0, hiện đang dùng) không phát hành `py.typed` — khác với
# pyiceberg và duckdb (CÓ, xem `[[tool.mypy.overrides]]` ở pyproject.toml gốc).
# `type: ignore` cục bộ ở đây thay vì thêm pyarrow vào danh sách bỏ qua toàn
# workspace, vì chỉ file này chạm trực tiếp vào kiểu của pyarrow.
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.table import Table


@dataclass(frozen=True, slots=True)
class TableInfo:
    namespace: str
    name: str

    @property
    def qualified(self) -> str:
        return f"{self.namespace}.{self.name}"


class DataFileWriter:
    """Ống ghi Parquet vào location của MỘT bảng, mở một lần rồi dùng lại.

    Tồn tại vì `Lakehouse.register_files` (tức `Table.add_files`) chỉ nhận những
    file ĐÃ GHI XONG, nên phải có một đường ghi Parquet TÁCH khỏi commit. Thăm dò
    `scripts/probe_iceberg_add_files.py` (Q4b) đã đo trên Lakekeeper thật: file
    chỉ đăng ký được khi nó nằm TRONG location của chính bảng — hai vị trí khác
    (trong warehouse nhưng ngoài bảng, và ngoài warehouse) đều hỏng, vì Lakekeeper
    vend credential STS hẹp theo TỪNG BẢNG. Vì vậy đường dẫn được dựng ở ĐÂY từ
    `table.location()` chứ không do người gọi tự đặt: một người gọi chọn sai chỗ
    sẽ nhận `ACCESS_DENIED`, một câu không nhắc gì tới location.

    **Giữ một `Table` đã nạp, có chủ đích.** `table.io` mang credential STS mà
    Lakekeeper VEND — đúng thứ pod nạp có trong tay, và cũng là thứ có HẠN. Nạp
    lại bảng cho từng file (hình dạng của `Lakehouse.append`) sẽ làm mới credential
    mỗi lô nhưng cộng một vòng REST cho mỗi lô, đúng loại chi phí cố định mà cả
    việc dùng `add_files` tồn tại để cắt (ĐO 3: commit catalog chiếm 44% đồng hồ
    tường). Nên writer được người gọi mở theo NHÓM và bỏ đi sau mỗi commit — xem
    `IcebergSink.commit`, chỗ quyết định tuổi tối đa của credential.

    Phần MÃ HOÁ Parquet là `pyarrow.parquet.write_table` THUẦN, nên file không có
    field ID của Iceberg. Đó không phải chỗ bỏ sót: Q4a đã đo rằng chính
    `add_files` ghi `schema.name-mapping.default` vào thuộc tính bảng, nên Iceberg
    nối cột theo TÊN và người gọi không phải làm gì thêm.
    """

    def __init__(self, table: Table) -> None:
        self._table = table

    def write(self, data: pa.Table, *, name: str) -> str:
        """Ghi MỘT file Parquet tên `name`, trả về URI để đưa cho `register_files`.

        `close()` trong `finally` chứ không phó mặc cho `write_table`: pyarrow chỉ
        đóng cái sink mà CHÍNH NÓ mở, nên một stream do người gọi đưa vào sẽ không
        được flush nếu không ai đóng — và một file Parquet thiếu footer đọc ra là
        "file quá ngắn", một câu không nhắc gì tới việc quên đóng.

        `overwrite=True` KHÔNG phải một cách để ghi đè dữ liệu: hai lần chạy khác
        nhau phải chọn hai `name` khác nhau (xem `IcebergSink._next_file_name`), và
        cờ này chỉ để một lần chạy đứt rồi chạy lại không vấp vào file rác của
        chính nó. Ghi đè một file ĐÃ ĐĂNG KÝ thì `add_files` không cứu được ai —
        nên tên file là thứ phải đúng, không phải cờ này.
        """
        uri = f"{self._table.location()}/data/{name}"
        out = self._table.io.new_output(uri).create(overwrite=True)
        try:
            pq.write_table(data, out)
        finally:
            out.close()
        return uri


class Lakehouse:
    """Một lakehouse, nhìn qua đúng một `RestCatalog`.

    KHÔNG giữ credential nào — mọi credential nằm trong `catalog` đã dựng
    sẵn (xem `build_catalog`). `Lakehouse` chỉ biết gọi PyIceberg và ép kết
    quả về Arrow.
    """

    def __init__(self, catalog: RestCatalog) -> None:
        self._catalog = catalog

    def list_namespaces(self) -> list[str]:
        return [".".join(ns) for ns in self._catalog.list_namespaces()]

    def create_namespace(self, namespace: str) -> None:
        self._catalog.create_namespace(namespace)

    def create_namespace_if_not_exists(self, namespace: str) -> None:
        """Như `create_namespace`, nhưng KHÔNG ném nếu `namespace` đã tồn tại.

        Giai đoạn 2c (CTAS trong `loom-query`): đích của một `CREATE TABLE ...
        AS SELECT` có thể nằm trong một namespace đã có (ghi thêm bảng thứ hai
        vào `bronze`) hay chưa (bảng đầu tiên của một namespace mới) — chỗ gọi
        không biết trước, và hỏi bằng `list_namespaces()` rồi mới `create_namespace`
        có điều kiện là hai round trip cho một câu PyIceberg tự trả lời được
        trong một (`create_namespace_if_not_exists` là API có sẵn của
        `RestCatalog`, không phải một lớp bọc thử-bắt tự viết ở đây).
        """
        self._catalog.create_namespace_if_not_exists(namespace)

    def drop_table(self, qualified: str) -> None:
        """Bỏ đăng ký `qualified` khỏi catalog.

        KHÔNG xoá data file trên S3 — đã kiểm bằng thực nghiệm trên Lakekeeper
        v0.9.2 thật (`scripts/measure_write_path.py`, dọn dẹp sau phép đo):
        `drop_table` + `drop_namespace` + xoá cả warehouse xong, `list_objects_v2`
        vẫn thấy nguyên data/metadata file dưới prefix của warehouse đó. Người
        gọi cần dọn ĐĨA THẬT (không chỉ catalog) phải tự xoá qua S3 sau bước này
        — xem `_purge_s3_prefix` trong `measure_write_path.py`.
        """
        self._catalog.drop_table(qualified)

    def drop_namespace(self, namespace: str) -> None:
        self._catalog.drop_namespace(namespace)

    def rename_table(self, from_qualified: str, to_qualified: str) -> None:
        """Đổi tên một bảng trong catalog. Dữ liệu KHÔNG bị chép lại.

        **Hai sự thật đã ĐO trên Lakekeeper v0.9.2 + PyIceberg 0.11.1 thật**
        (Đo 2 mục D, `scripts/probe_iceberg_single_commit.py` — đọc phần "KẾT
        QUẢ D ĐÃ GHI NHẬN" ở docstring script đó), vì cả hai quyết định cách
        người gọi phải dùng phương thức này:

        - **Đây là MOVE, không phải COPY** (D3): sau lời gọi, `from_qualified`
          ném `NoSuchTableError`. Dữ liệu nguyên vẹn dưới tên mới (D2:
          1000/1000 dòng khớp cả hai cột).
        - **ĐÈ lên một tên ĐANG TỒN TẠI bị TỪ CHỐI** (D4:
          `TableAlreadyExistsError`). Nên "thay bảng X bằng nội dung của bảng
          Y" KHÔNG viết được thành một lời gọi duy nhất — nó phải là một chuỗi
          nhiều bước, và chuỗi đó có một cửa sổ mà tên đích không phân giải
          được. `loom_task.sink.IcebergSink` là chỗ dựng chuỗi ba bước đó, kèm
          lý do vì sao ba chứ không hai.

        KHÔNG bọc lỗi lại: một `TableAlreadyExistsError` từ PyIceberg nói đúng
        chuyện đã xảy ra, và người gọi (chuỗi tráo bảng của `full`) cần phân
        biệt được nó với một lỗi mạng.
        """
        self._catalog.rename_table(from_qualified, to_qualified)

    def list_tables(self, namespace: str) -> list[TableInfo]:
        return [
            TableInfo(namespace=".".join(identifier[:-1]), name=identifier[-1])
            for identifier in self._catalog.list_tables(namespace)
        ]

    def schema(self, qualified: str) -> pa.Schema:
        return self._catalog.load_table(qualified).schema().as_arrow()

    def exists(self, qualified: str) -> bool:
        # `table_exists` được tenacity `@retry` bọc lại, và điều đó xoá mất
        # chú thích kiểu trả về trong mắt mypy — `bool(...)` phục hồi nó.
        return bool(self._catalog.table_exists(qualified))

    def create_from(self, qualified: str, data: pa.Table, *, replace: bool = False) -> None:
        """Tạo `qualified` từ đầu, nạp `data` làm data file đầu tiên.

        `replace=False` (mặc định, hành vi CŨ — không đổi): `qualified` đã tồn
        tại thì ném lỗi THẬT từ PyIceberg — không nuốt, không đoán ý người gọi.

        `replace=True` (Giai đoạn 2c, `CREATE OR REPLACE TABLE ... AS SELECT`
        trong `loom-query`): XOÁ bảng cũ (nếu có) rồi tạo lại — KHÔNG atomic
        (hai lệnh riêng, không một transaction/staged commit), đủ cho phạm vi
        Giai đoạn 2c: chưa có ai đọc đồng thời một bảng đang bị CTAS ghi đè.
        Một bản atomic hơn (`create_table_transaction`/`commit_table`) là việc
        của một task sau nếu spec đòi.
        """
        if replace and self.exists(qualified):
            self._catalog.drop_table(qualified)
        table = self._catalog.create_table(qualified, schema=data.schema)
        table.append(data)

    def append(self, qualified: str, data: pa.Table) -> None:
        self._catalog.load_table(qualified).append(data)

    def create_empty(self, qualified: str, schema: pa.Schema) -> None:
        """Tạo `qualified` KHÔNG có dòng nào, schema Iceberg sinh từ `schema` Arrow.

        Khác `create_from` ở đúng một điểm, và điểm đó là cả lý do nó tồn tại:
        `create_from` nối luôn dữ liệu (một commit), còn đường `add_files` cần bảng
        có mặt TRƯỚC khi ghi file — `DataFileWriter` dựng đường dẫn từ
        `table.location()`, và một bảng chưa tồn tại thì chưa có location.

        Bảng rỗng ở đây KHÔNG có snapshot nào: `register_files` sau đó thêm ĐÚNG
        MỘT snapshot cho cả nhóm file (đã đo với N = 1, 5, 20 —
        `scripts/probe_iceberg_add_files.py` Q1).
        """
        self._catalog.create_table(qualified, schema=schema)

    def data_file_writer(self, qualified: str) -> DataFileWriter:
        """Mở một `DataFileWriter` cho `qualified` — MỘT vòng REST, dùng cho N file.

        Xem `DataFileWriter` cho lý do người gọi nên giữ nó theo NHÓM lô thay vì
        mở lại cho từng lô, và cho cái giá của việc giữ (credential STS có hạn).
        """
        return DataFileWriter(self._catalog.load_table(qualified))

    def register_files(self, qualified: str, uris: Sequence[str]) -> None:
        """Đăng ký N file Parquet đã ghi vào `qualified` bằng ĐÚNG MỘT commit.

        Đây là phép thay cho "một `append` mỗi lô". Đã đo trên Lakekeeper v0.9.2 +
        PyIceberg 0.11.1 thật (`scripts/probe_iceberg_add_files.py`): N file vào
        ĐÚNG 1 snapshot với N = 1, 5, 20, và thời gian commit PHẲNG theo N
        (0,56 / 0,62 / 0,61 s). So với 50 lần `append`: 3,2 s thay vì 47,9 s, đỉnh
        RSS 173 thay vì 281 MiB.

        **`check_duplicate_files=True` viết TƯỜNG MINH dù nó là mặc định của
        PyIceberg.** Không phải để trang trí: cùng phép thăm dò (Q4c) đã đăng ký
        LẠI một file với cờ tắt và bảng đi từ 1000 lên 2000 dòng, không lỗi, không
        dấu vết. Một ngày nào đó có người thấy phép kiểm này tốn thời gian (nó đọc
        MỌI manifest của bảng, nên giá của nó lớn dần theo bảng) và muốn tắt — dòng
        này cùng đoạn chú thích này là thứ họ phải đọc trước.

        KHÔNG nuốt lỗi: đăng ký lại một file đã có sẽ ném, và đó là hành vi mong
        muốn — người gọi (`IcebergSink`) đặt tên file theo `run_id` + số thứ tự nên
        một lần trùng tên là một lỗi trong cách đặt tên, không phải một sự cố cần
        bỏ qua.
        """
        self._catalog.load_table(qualified).add_files(list(uris), check_duplicate_files=True)

    def scan(self, qualified: str) -> pa.RecordBatchReader:
        """Trả một reader theo LUỒNG, KHÔNG một bảng đã nạp hết vào RAM.

        Một bảng vài chục GB nạp hết vào RAM trước khi DuckDB thấy dòng đầu là
        cách chắc chắn bị OOMKill: pod query ở Giai đoạn 2b chỉ có 384 MiB, và
        đo thật (`packages/icebergkit/tests/test_duckdb_memory.py`) cho thấy
        biên chỉ khoảng 8 MiB. `to_arrow_batch_reader()` phát từng batch theo
        yêu cầu của bên đọc, thay vì vật chất hoá toàn bộ bảng trước.
        """
        return self._catalog.load_table(qualified).scan().to_arrow_batch_reader()

    def scan_size_bytes(self, qualified: str) -> int:
        """Tổng byte của MỌI data file mà một scan không lọc sẽ đọc — lấy
        THẲNG từ thống kê manifest Iceberg, KHÔNG mở một data file nào.

        Thăm dò (`services/loom-query`, Task 8, trên PyIceberg 0.11.1 thật với
        Lakekeeper thật): `Table.scan()` trả một `DataScan`; `.plan_files()`
        trả `Iterable[FileScanTask]`; mỗi `FileScanTask.file` là một
        `DataFile` với thuộc tính `.file_size_in_bytes` — đúng con số đã nằm
        sẵn trong manifest (Iceberg ghi nó lúc commit), không phải một phép đo
        cần mở file để tính. `plan_files()` tự nó chỉ đọc manifest list và
        manifest file (metadata, nhỏ và không co giãn theo kích thước bảng) —
        KHÔNG chạm data file nào, nên gọi hàm này trước một `scan()` (đọc
        thật) là kiểm được trần byte quét TRƯỚC khi tốn một request nào tới
        chính data file.

        Không lọc (giống hệt `scan()` ở trên): quét trần phải là quét THẬT sự
        engine sẽ làm, và `Lakehouse.scan()` không nhận filter/projection.
        """
        table = self._catalog.load_table(qualified)
        return sum(task.file.file_size_in_bytes for task in table.scan().plan_files())
