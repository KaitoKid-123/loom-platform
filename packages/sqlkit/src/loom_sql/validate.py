"""Kiểm cú pháp SQL thuần qua AST của sqlglot. Không I/O, không chạy SQL."""

from __future__ import annotations

import sqlglot
from sqlglot.errors import ParseError

from loom_sql.errors import SqlError


def validate(sql: str, dialect: str) -> list[SqlError]:
    """Rỗng nghĩa là hợp lệ.

    `sqlglot.parse` ném `ParseError` với thuộc tính `.errors`: danh sách dict có
    khóa `description`/`line`/`col`. Xác minh bằng thăm dò trực tiếp trên
    sqlglot 30.15.0 (Bước 0 của Task 3-5): cả `line` lẫn `col` đã LÀ 1-based —
    dòng đầu tiên báo `line=1`, cột đầu tiên của một dòng báo `col=1` — nên
    KHÔNG cần dịch offset ở đây. Nếu một bản sqlglot sau đổi sang 0-based, phép
    kiểm dòng-3 trong `test_validate.py` sẽ đỏ và bắt được ngay.
    """
    try:
        sqlglot.parse(sql, read=dialect)
    except ParseError as e:
        return [
            SqlError(message=err["description"], line=err["line"], column=err["col"])
            for err in e.errors
        ]
    return []
