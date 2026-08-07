"""Đọc/viết bảng Iceberg qua một `RestCatalog`, chỉ nói Apache Arrow ra ngoài.

Mọi giá trị `Lakehouse` trả về là Arrow (`pa.Schema`, `pa.RecordBatchReader`),
không phải kiểu của PyIceberg (`Schema`, `Table`, `DataScan`...). Đó là điều
làm cho việc đổi engine ở spec v1 mục 5.9 khả thi thay vì chỉ là một lời hứa:
chỗ gọi `Lakehouse` không cần biết PyIceberg tồn tại.
"""

from dataclasses import dataclass

# pyarrow (25.0.0, hiện đang dùng) không phát hành `py.typed` — khác với
# pyiceberg và duckdb (CÓ, xem `[[tool.mypy.overrides]]` ở pyproject.toml gốc).
# `type: ignore` cục bộ ở đây thay vì thêm pyarrow vào danh sách bỏ qua toàn
# workspace, vì chỉ file này chạm trực tiếp vào kiểu của pyarrow.
import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.catalog.rest import RestCatalog


@dataclass(frozen=True, slots=True)
class TableInfo:
    namespace: str
    name: str

    @property
    def qualified(self) -> str:
        return f"{self.namespace}.{self.name}"


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
