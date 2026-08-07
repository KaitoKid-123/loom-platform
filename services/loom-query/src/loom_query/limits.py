"""Giới hạn 2 và 3 của Task 8 (byte quét, dòng trả về) — thuần Python.

`memory_limit`/`threads` (giới hạn 4, 5) GHIM CỨNG trong `runner.py`, không ở
đây (xem docstring `runner.py` cho lý do). `runner.py` gọi cả hai hàm dưới đây
trong `_run_sync`; file này tách riêng để kiểm được không cần DuckDB — pyarrow
và một `ScanStats` giả là đủ cho `truncate_table`/`check_scan_bytes`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

# pyarrow không phát hành `py.typed` — cùng lý do `type: ignore` cục bộ đã
# dùng ở `runner.py`/`loom_iceberg.lakehouse`.
import pyarrow as pa  # type: ignore[import-untyped]


class ScanStats(Protocol):
    """Thứ `check_scan_bytes` cần từ một lakehouse — CHỈ một phép hỏi thống kê,
    không phải toàn bộ `Lakehouse`. `loom_iceberg.Lakehouse.scan_size_bytes`
    khớp Protocol này về mặt cấu trúc; test không cần dựng một catalog thật để
    kiểm phép cộng dồn và phép so trần (xem `tests/test_query_limits.py`).
    """

    def scan_size_bytes(self, qualified: str) -> int: ...


class ScanBytesExceeded(Exception):
    """Ném TRƯỚC KHI `Lakehouse.scan()` (đọc thật) được gọi cho BẤT KỲ bảng
    nào trong câu — xem `check_scan_bytes`. `runner.execute` bắt nó như mọi
    ngoại lệ khác và biến thành một query `failed`, không phải một mã lỗi
    HTTP riêng: response của `POST` đã trả `202` từ lâu lúc lỗi này xảy ra.
    """

    def __init__(self, scanned_bytes: int, max_bytes: int) -> None:
        self.scanned_bytes = scanned_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"query would scan {scanned_bytes:,} bytes, "
            f"over the {max_bytes:,} byte cap — rejected before reading any data"
        )


def check_scan_bytes(tables: Iterable[tuple[ScanStats, str]], max_bytes: int) -> int:
    """Cộng byte của MỌI bảng trong `tables` từ thống kê manifest Iceberg,
    KHÔNG đọc một byte dữ liệu nào (xem `Lakehouse.scan_size_bytes`). Ném
    `ScanBytesExceeded` nếu tổng vượt `max_bytes`; trả tổng khi không vượt.

    `tables` là `(lakehouse, qualified)` — MỖI bảng kèm ĐÚNG lakehouse (catalog
    Iceberg) sở hữu nó, không phải một `lakehouse` duy nhất cho tất cả: một
    `JOIN` hai lakehouse (xem `runner.py`) phải cộng byte quét trên ĐÚNG catalog
    của từng bảng, và `qualified` phải là tên THẬT trên catalog đó (namespace
    ĐÃ bỏ tiền tố alias ba phần nếu có — xem `authz.strip_lakehouse_alias`).
    Hàm này vì vậy không cần biết gì về `TableRef`/quy ước đặt tên bảng nữa —
    người gọi (`runner.py`) đã tính xong cặp (lakehouse, tên thật) trước khi
    gọi tới đây.

    Cộng dồn CẢ CÂU trước khi so sánh — không so từng bảng riêng lẻ — vì một
    `JOIN` hai bảng nhỏ-vừa-đủ-lọt-trần-riêng vẫn có thể vượt trần khi cộng
    lại, và đó chính xác là byte engine sẽ đọc nếu để lọt qua. Đúng với cả khi
    hai bảng đó nằm ở HAI lakehouse khác nhau — trần áp cho CẢ CÂU, không phải
    cho từng lakehouse riêng.
    """
    total = sum(lakehouse.scan_size_bytes(qualified) for lakehouse, qualified in tables)
    if total > max_bytes:
        raise ScanBytesExceeded(total, max_bytes)
    return total


def truncate_table(table: pa.Table, max_rows: int) -> tuple[pa.Table, bool, int]:
    """Cắt kết quả xuống `max_rows` dòng ĐẦU, luôn trả tổng số dòng THẬT.

    Trả `(bảng đã cắt, đã cắt hay chưa, tổng số dòng trước khi cắt)`. Không có
    cờ `truncated`, 10.000 dòng đầu trông y hệt toàn bộ kết quả — người dùng
    (và một báo cáo dựa trên nó) sẽ không biết phần còn lại đã bị bỏ.
    """
    total = table.num_rows
    if total <= max_rows:
        return table, False, total
    return table.slice(0, max_rows), True, total
