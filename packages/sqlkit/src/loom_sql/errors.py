"""Kiểu dữ liệu lỗi cú pháp SQL — dùng chung bởi `validate` và (sau này) editor
Giai đoạn 2c để gạch đỏ đúng chỗ trong ô nhập SQL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SqlError:
    """Một lỗi cú pháp, định vị theo dòng/cột 1-based (dòng đầu = 1, cột đầu = 1)."""

    message: str
    line: int
    column: int
