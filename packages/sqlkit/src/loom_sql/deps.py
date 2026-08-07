"""Trích danh sách bảng THẬT một câu SQL chạm tới — chỗ RBAC gặp SQL.

Giai đoạn 2b kiểm quyền đúng trên tập bảng `table_deps` trả về, không hơn không
kém: bỏ sót một bảng nghĩa là bảng đó đi vòng qua toàn bộ RBAC; trả thừa một CTE
nghĩa là từ chối người dùng vì một bảng họ không hề đọc.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass(frozen=True, slots=True)
class TableRef:
    namespace: str | None  # None khi SQL không nêu namespace (catalog/schema)
    name: str


def table_deps(sql: str, dialect: str) -> list[TableRef]:
    """Mọi bảng THẬT mà câu lệnh chạm tới.

    Đã khử trùng lặp, thứ tự ỔN ĐỊNH (sắp xếp theo namespace rồi tên) — KHÔNG
    phải thứ tự xuất hiện trong câu SQL. Chỗ gọi so sánh danh sách này; một thứ
    tự đổi theo cách sqlglot duyệt AST giữa các bản sẽ làm test đỏ ở nơi không
    liên quan tới `table_deps`.

    CTE không phải bảng thật: xác nhận bằng thăm dò trên sqlglot 30.15.0 (Bước 0)
    rằng `find_all(exp.Table)` trả về CẢ bí danh CTE lẫn bảng thật nó bọc quanh,
    nên phải loại tên trùng bí danh CTE ra khỏi kết quả. Lọc theo TÊN (không
    theo scope): nếu một CTE và một bảng thật trùng tên, CTE che bảng thật
    trong phạm vi câu lệnh đó — đúng ngữ nghĩa SQL chuẩn — nên loại theo tên là
    đúng, xem `test_cte_shadows_real_table_of_same_name`.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}

    refs: set[TableRef] = set()
    for table in tree.find_all(exp.Table):
        if table.name in cte_names:
            continue
        parts = [p for p in (table.catalog, table.db) if p]
        namespace = ".".join(parts) if parts else None
        refs.add(TableRef(namespace=namespace, name=table.name))

    return sorted(refs, key=lambda r: (r.namespace or "", r.name))
