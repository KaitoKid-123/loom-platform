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
        statements = sqlglot.parse(sql, read=dialect)
    except ParseError as e:
        return [
            SqlError(message=err["description"], line=err["line"], column=err["col"])
            for err in e.errors
        ]

    # MỘT câu lệnh mỗi lần, và đây là một hàng rào QUYỀN chứ không phải một quy
    # ước phong cách.
    #
    # `dependencies()` gọi `parse_one`, và với nhiều câu lệnh sqlglot 30.15.0 trả
    # về một `exp.Block`. `_write_destination(Block)` không khớp `exp.Create` lẫn
    # `exp.Insert` nên trả `None` — thế là đích GHI bị xếp nhầm thành một bảng
    # ĐỌC. Hậu quả đo được:
    #
    #     dependencies("SELECT 1; CREATE TABLE ns.t AS SELECT 1")
    #        -> writes=[]  reads=[ns.t]
    #
    # `run_gate` khi đó chỉ đòi `item_read` (viewer) cho một câu lệnh GHI, trong
    # khi `ACTION_MATRIX` đặt `item.update` ở `contributor`. Và DuckDB thì CÓ
    # chạy cả chuỗi câu lệnh khi nhận một chuỗi như vậy.
    #
    # Hôm nay chưa khai thác được tới một lần ghi Iceberg thật — runner đăng ký
    # bảng nguồn dưới dạng view nên các biến thể ghi đều chết trong DuckDB —
    # nhưng đó là một tai nạn về triển khai, không phải một hàng rào. Chặn ở đây
    # thì fail-closed: `run_gate` chạy `validate()` TRƯỚC `dependencies()`, nên
    # không có quyết định phân quyền nào được đưa ra dựa trên một cây bị đọc sai.
    if len(statements) > 1:
        return [
            SqlError(
                message=(
                    "Chỉ chạy được MỘT câu lệnh mỗi lần; "
                    f"nhận được {len(statements)}. Bỏ dấu chấm phẩy ở giữa."
                ),
                line=1,
                column=1,
            )
        ]
    return []
