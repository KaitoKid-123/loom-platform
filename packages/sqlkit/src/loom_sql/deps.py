"""Trích phụ thuộc của một câu SQL — chỗ RBAC gặp SQL.

Giai đoạn 2b kiểm quyền đúng trên tập `Dependencies.tables` trả về, không hơn
không kém: bỏ sót một bảng nghĩa là bảng đó đi vòng qua toàn bộ RBAC; trả thừa
một CTE nghĩa là từ chối người dùng vì một bảng họ không hề đọc.

**Vì sao trả về `Dependencies` chứ không phải một danh sách bảng.** SQL đọc dữ
liệu được từ những chỗ KHÔNG phải bảng trong catalog:

    SELECT * FROM read_parquet('s3://workspace-khac/bi-mat.parquet')
    SELECT * FROM 's3://workspace-khac/**/*.parquet'
    SELECT * FROM range(10)

sqlglot phân tích cả ba thành `exp.Table` — hai cái đầu là **đường đọc dữ liệu
thật sự đi vòng qua catalog**, cái thứ ba vô hại. Nếu hàm này chỉ trả một danh
sách bảng thì cả ba lẫn lộn vào đó dưới dạng tên rỗng hoặc tên là một đường dẫn.

Điều đó *tình cờ* fail closed hôm nay: tên rỗng và đường dẫn lạ không phân giải
được thành item nên bị từ chối. Nhưng nó đóng do tình cờ, và có một cái bẫy cụ
thể — người dọn dẹp thấy `TableRef(name='')` là nhiễu, lọc nó đi, và lúc đó
`SELECT * FROM read_parquet('s3://…')` có **zero** phụ thuộc bảng. Một query
không chạm bảng nào thì rất dễ được cho qua.

Việc dọn hiển nhiên biến fail-closed thành fail-open. Nên chúng được TÁCH RA
thành `external`, và chỗ gọi buộc phải nhìn thấy chúng.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass(frozen=True, slots=True)
class TableRef:
    namespace: str | None  # None khi SQL không nêu namespace (catalog/schema)
    name: str


@dataclass(frozen=True, slots=True)
class Dependencies:
    """Bảng thật, và mọi thứ đọc dữ liệu mà KHÔNG qua catalog.

    `external` rỗng là điều kiện cần để một query an toàn ở Giai đoạn 2b — đường
    đọc file thô trong `Files/` chưa được xây, nên chưa có gì phân quyền cho nó.
    """

    tables: list[TableRef]
    external: list[str]


# Hàm bảng đọc được dữ liệu ngoài catalog. KHÔNG phải danh sách đầy đủ, và không
# cần đầy đủ: mọi `exp.Table` không có tên định danh đều rơi vào `external`, nên
# một hàm mới của DuckDB cũng bị bắt. Danh sách này chỉ để THÔNG BÁO nói được
# tên thứ đã bị chặn.
_KNOWN_READERS = frozenset(
    {"read_parquet", "read_csv", "read_csv_auto", "read_json", "read_json_auto", "parquet_scan"}
)


def _looks_like_a_path(name: str) -> bool:
    """DuckDB cho `SELECT * FROM 's3://…/*.parquet'` — một đường dẫn ở vị trí bảng.

    sqlglot trả nó về như một `exp.Table` mang nguyên đường dẫn làm tên, nên nếu
    chỉ nhìn "có tên hay không" thì nó lọt qua như một bảng bình thường.
    """
    return "://" in name or name.startswith(("/", "./", "../")) or "*" in name


def dependencies(sql: str, dialect: str) -> Dependencies:
    """Tách phụ thuộc thành bảng catalog và nguồn đọc ngoài catalog.

    `tables` đã khử trùng lặp, thứ tự ỔN ĐỊNH (sắp theo namespace rồi tên) —
    KHÔNG phải thứ tự xuất hiện. Chỗ gọi so sánh danh sách này; một thứ tự đổi
    theo cách sqlglot duyệt AST giữa các bản sẽ làm test đỏ ở nơi không liên quan.

    CTE không phải bảng thật: thăm dò trên sqlglot 30.15.0 xác nhận
    `find_all(exp.Table)` trả về CẢ bí danh CTE lẫn bảng thật nó bọc quanh, nên
    phải loại tên trùng bí danh CTE. Lọc theo TÊN, không theo scope: nếu một CTE
    và một bảng thật trùng tên thì CTE che bảng thật trong phạm vi câu lệnh đó —
    đúng ngữ nghĩa SQL chuẩn.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}

    tables: set[TableRef] = set()
    external: set[str] = set()

    for table in tree.find_all(exp.Table):
        name = table.name

        # Không có tên định danh: hàm bảng như `range(10)` hay
        # `read_parquet('…')`. sqlglot đặt lời gọi hàm vào `table.this`.
        if not name:
            call = table.this
            label = call.sql_name() if isinstance(call, exp.Func) else str(call)
            external.add(label)
            continue

        if _looks_like_a_path(name):
            external.add(name)
            continue

        if name in cte_names:
            continue

        parts = [p for p in (table.catalog, table.db) if p]
        tables.add(TableRef(namespace=".".join(parts) if parts else None, name=name))

    return Dependencies(
        tables=sorted(tables, key=lambda r: (r.namespace or "", r.name)),
        external=sorted(external),
    )


def table_deps(sql: str, dialect: str) -> list[TableRef]:
    """Chỉ các bảng catalog.

    Giữ lại vì nó là bề mặt hẹp, tiện cho việc dựng lineage ở Giai đoạn 4. **Chỗ
    kiểm quyền KHÔNG được dùng hàm này** — nó bỏ qua `external`, và bỏ qua
    `external` là bỏ qua đúng những đường đọc dữ liệu không đi qua catalog.
    """
    return dependencies(sql, dialect).tables
