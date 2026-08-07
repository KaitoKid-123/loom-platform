"""`loom_query.limits` — thuần Python, KHÔNG cần Docker.

`ScanStats` là một Protocol (xem `limits.py`) nên `check_scan_bytes` kiểm được
ở đây bằng một fake nhỏ, không cần dựng một `Lakehouse`/catalog thật —
`tests/integration/test_query_scan_bytes.py` mới là nơi kiểm với Iceberg THẬT
(và là nơi chứng minh đỏ 1 của Task 8: chuyển phép kiểm xuống SAU khi quét).
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from loom_query.limits import ScanBytesExceeded, check_scan_bytes, truncate_table


class FakeScanStats:
    """`ScanStats` giả — trả đúng byte mà test cấp trước, không chạm mạng."""

    def __init__(self, sizes: dict[str, int]) -> None:
        self._sizes = sizes

    def scan_size_bytes(self, qualified: str) -> int:
        return self._sizes[qualified]


def test_check_scan_bytes_sums_every_table_in_the_query() -> None:
    """Một `JOIN` hai bảng: trần phải soi TỔNG, không phải bảng lớn nhất một
    mình — hai bảng 4.000 byte cùng lọt trần riêng lẻ nhưng CỘNG lại (8.000)
    thì không, nếu trần là 6.000."""
    stats = FakeScanStats({"ns.a": 4_000, "ns.b": 4_000})

    with pytest.raises(ScanBytesExceeded) as exc_info:
        check_scan_bytes([(stats, "ns.a"), (stats, "ns.b")], max_bytes=6_000)

    assert exc_info.value.scanned_bytes == 8_000
    assert exc_info.value.max_bytes == 6_000


def test_check_scan_bytes_under_the_cap_returns_the_total_without_raising() -> None:
    stats = FakeScanStats({"ns.a": 1_000, "ns.b": 2_000})

    total = check_scan_bytes([(stats, "ns.a"), (stats, "ns.b")], max_bytes=10_000)

    assert total == 3_000


def test_check_scan_bytes_at_exactly_the_cap_is_allowed() -> None:
    """Biên: đúng bằng trần thì KHÔNG bị từ chối — trần là "tối đa", không phải
    "nhỏ hơn". Một cài đặt lỡ dùng `>=` thay vì `>` sẽ đỏ ở đây."""
    stats = FakeScanStats({"ns.a": 10_000})

    total = check_scan_bytes([(stats, "ns.a")], max_bytes=10_000)

    assert total == 10_000


def test_check_scan_bytes_sums_across_two_different_lakehouses() -> None:
    """`JOIN` hai LAKEHOUSE khác nhau (hai `ScanStats` khác nhau, một cho mỗi
    catalog): trần vẫn áp cho CẢ CÂU, cộng dồn qua cả hai catalog — không phải
    một trần riêng cho mỗi lakehouse."""
    stats_a = FakeScanStats({"ns.a": 4_000})
    stats_b = FakeScanStats({"ns.a": 4_000})  # tên trùng CỐ Ý — hai catalog riêng

    with pytest.raises(ScanBytesExceeded) as exc_info:
        check_scan_bytes([(stats_a, "ns.a"), (stats_b, "ns.a")], max_bytes=6_000)

    assert exc_info.value.scanned_bytes == 8_000


def test_scan_bytes_exceeded_message_names_both_numbers() -> None:
    exc = ScanBytesExceeded(scanned_bytes=12_000, max_bytes=10_000)
    assert "12,000" in str(exc)
    assert "10,000" in str(exc)


def test_truncate_table_under_the_cap_is_not_truncated() -> None:
    table = pa.table({"i": pa.array(range(5))})

    limited, truncated, total = truncate_table(table, max_rows=10)

    assert truncated is False
    assert total == 5
    assert limited.num_rows == 5


def test_truncate_table_at_exactly_the_cap_is_not_truncated() -> None:
    table = pa.table({"i": pa.array(range(10))})

    limited, truncated, total = truncate_table(table, max_rows=10)

    assert truncated is False
    assert total == 10
    assert limited.num_rows == 10


def test_truncate_table_over_the_cap_is_truncated_and_reports_the_real_total() -> None:
    table = pa.table({"i": pa.array(range(25))})

    limited, truncated, total = truncate_table(table, max_rows=10)

    assert truncated is True
    assert total == 25
    assert limited.num_rows == 10
    # Cắt DÒNG ĐẦU, không phải một mẫu ngẫu nhiên hay dòng cuối.
    assert limited.column("i").to_pylist() == list(range(10))
