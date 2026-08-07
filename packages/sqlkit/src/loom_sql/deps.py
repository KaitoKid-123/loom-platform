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

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

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


# --- Task 13 (Giai đoạn 2b): đọc file thô trong `Files/` ---------------------
#
# `Dependencies.external` ở trên chỉ giữ một NHÃN cho mỗi nguồn ngoài catalog —
# đủ để BÁO ("query này chạm một thứ ngoài catalog") nhưng không đủ để
# `loom_query` phục vụ một khe hẹp trong đó (đọc `Files/` của lakehouse qua
# `read_parquet`/`read_csv` — xem `loom_query.files`). Phần dưới đây vì vậy
# tách thành các hàm RIÊNG, không sửa `dependencies()`/`Dependencies.external`:
# hình dạng đó đã có test riêng ở trên cho MỌI nguồn ngoài catalog, không chỉ
# hai hàm đọc file — đổi nó để nhét thêm path sẽ kéo theo những test không hề
# cần biết `read_parquet` là gì.
#
# HAI HÀM DUY NHẤT được phục vụ — `read_csv_auto`, `read_json`, `read_json_auto`,
# `parquet_scan` (bốn cái còn lại trong `_KNOWN_READERS`) vẫn chỉ lộ ra ở
# `external` như trước, KHÔNG có mặt ở đây. Nới rộng hơn hai hàm này là quyết
# định của một task khác — xem báo cáo hoàn tất Task 13 cho lý do dừng ở đúng
# hai hàm mà spec liệt kê.
FILE_READ_FUNCTIONS = frozenset({"read_parquet", "read_csv"})


@dataclass(frozen=True, slots=True)
class FileReadCall:
    """Một lời gọi `read_parquet`/`read_csv`, cùng path literal ở đối số ĐẦU.

    `paths` rỗng nếu đối số đầu không phải một literal chuỗi hay một mảng toàn
    literal chuỗi (biến, cột, biểu thức nối chuỗi...) — chỗ gọi
    (`loom_query.files`) coi rỗng là KHÔNG kiểm được, và từ chối, không phải bỏ
    qua: một path không xác định được lúc phân tích tĩnh thì không thể chứng
    minh nó nằm trong `Files/` của đúng lakehouse nào.
    """

    function: str
    paths: tuple[str, ...]


def _reader_name(call: exp.Func) -> str:
    """Tên hàm THẬT của `call`, chữ thường.

    `call.sql_name()` trả `"ANONYMOUS"` cho các hàm sqlglot không có class
    riêng (bốn cái trong `_KNOWN_READERS` không nằm trong `FILE_READ_FUNCTIONS`
    — đã kiểm bằng thực nghiệm sqlglot 30.15.0); tên thật của chúng nằm ở
    `call.this`, một CHUỖI (không phải `exp.Expression`) khi `call` là
    `exp.Anonymous`. `read_parquet`/`read_csv` THÌ có class riêng
    (`exp.ReadParquet`/`exp.ReadCSV`) nên `sql_name()` đã đúng, không cần rẽ
    nhánh — nhưng vẫn kiểm `exp.Anonymous` trước để không phụ thuộc việc hai
    hàm này mãi mãi có class riêng ở một bản sqlglot sau này.
    """
    if isinstance(call, exp.Anonymous):
        return str(call.this).lower()
    return call.sql_name().lower()


def _path_argument(call: exp.Func) -> exp.Expression | None:
    """Đối số MANG PATH của `call` — vị trí khác nhau giữa hai class, đã kiểm
    bằng thực nghiệm (sqlglot 30.15.0, DuckDB 1.5.5):

    - `exp.ReadCSV`: path luôn ở `call.this`, TÁCH RIÊNG khỏi `call.
      expressions` (tuỳ chọn như `delim=','` nằm ở đó, không lẫn vào).
    - `exp.ReadParquet`: KHÔNG có `this` — path là PHẦN TỬ ĐẦU của `call.
      expressions`; tuỳ chọn (`hive_partitioning=true`) là các phần tử SAU,
      trong CÙNG một list. Phải lấy đúng phần tử đầu theo VỊ TRÍ, không lọc
      theo kiểu `exp.Literal` trong toàn bộ list: một tuỳ chọn dạng chuỗi đứng
      sau path (hiếm nhưng hợp lệ cú pháp) trông giống hệt một path thứ hai
      nếu chỉ lọc theo kiểu.
    """
    if isinstance(call, exp.ReadCSV):
        # `exp.Expression.this` khai `Any` trong sqlglot (thuộc tính động,
        # không có kiểu tĩnh riêng cho từng subclass) — `cast` ở đây là trung
        # thực với thực nghiệm đã kiểm (luôn là `exp.Literal`/`exp.Array` cho
        # `exp.ReadCSV`), không phải một cách né mypy.
        return cast(exp.Expression, call.this)
    if isinstance(call, exp.ReadParquet):
        expressions = call.args.get("expressions") or []
        return expressions[0] if expressions else None
    return None


def _string_literals(node: exp.Expression | None) -> tuple[str, ...]:
    """`node` -> literal chuỗi của nó.

    MỘT phần tử nếu `node` chính là một `exp.Literal` chuỗi; NHIỀU phần tử nếu
    `node` là một `exp.Array` mà MỌI phần tử đều là literal chuỗi; RỖNG cho bất
    kỳ hình dạng nào khác. Một mảng TRỘN literal và biến bị coi là hoàn toàn
    không kiểm được (rỗng), không phải "lấy phần literal, bỏ phần kia" — trộn
    kiểu đó không phải cú pháp DuckDB thật cho `read_parquet`/`read_csv`, và cố
    phân xử nó chỉ thêm một nhánh không ai kiểm được.
    """
    if isinstance(node, exp.Literal) and node.is_string:
        return (node.this,)
    if isinstance(node, exp.Array):
        items = node.expressions
        if items and all(isinstance(item, exp.Literal) and item.is_string for item in items):
            return tuple(item.this for item in items)
        return ()
    return ()


def file_read_calls(sql: str, dialect: str) -> list[FileReadCall]:
    """Mọi lời gọi `read_parquet`/`read_csv` trong `sql`, cùng path literal ở
    đối số đầu — xem `FileReadCall`.

    Một PHÉP DUYỆT RIÊNG, KHÔNG tái dùng cây của `dependencies()`: hai hàm trả
    lời hai câu hỏi khác nhau (`dependencies` hỏi "bảng nào bị kiểm quyền", hàm
    này hỏi "đối số path nào cần kiểm an toàn") — gọi thêm một lần `sqlglot.
    parse_one` rẻ hơn nhiều so với một API dùng chung cố ép hai câu hỏi vào
    cùng một hình dạng.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    calls: list[FileReadCall] = []
    for table in tree.find_all(exp.Table):
        if table.name:  # bảng thật, hoặc một path trần (`FROM 's3://…'`) — bỏ.
            continue
        call = table.this
        if not isinstance(call, exp.Func):
            continue
        name = _reader_name(call)
        if name not in FILE_READ_FUNCTIONS:
            continue
        calls.append(FileReadCall(function=name, paths=_string_literals(_path_argument(call))))
    return calls


def rewrite_file_reads(sql: str, dialect: str, resolve: Callable[[str], str]) -> str:
    """Viết lại `sql`: mọi literal chuỗi ở đối số path của một lời gọi
    `read_parquet`/`read_csv` (xem `file_read_calls`) được thay bằng
    `resolve(literal_gốc)`. Trả về SQL cùng dialect, tái sinh từ AST.

    `resolve` là hàm PHÍA GỌI cấp — hàm này không biết gì về workspace/
    lakehouse/S3 (package `loom_sql` không I/O, xem docstring đầu file); đó là
    việc của `loom_query.files.resolve_files_query`, hàm DUY NHẤT gọi tới đây
    trong toàn bộ hệ thống. `resolve` được phép ném lỗi (một đường dẫn không
    an toàn) — lỗi đó KHÔNG bị nuốt ở đây, truyền thẳng lên chỗ gọi.

    KHÔNG sửa các đối số KHÁC của lời gọi (`hive_partitioning=true`,
    `delim=','`...) — chỉ đúng (các) node mà `_path_argument`/`_string_literals`
    trỏ tới bị `.set()` tại chỗ, mọi thứ khác trong `tree` giữ nguyên. Một lời
    gọi mà `_path_argument` không trỏ tới một literal/mảng-literal (path động,
    không kiểm được) bị BỎ QUA ở đây — chỗ gọi (`resolve_files_query`) đã từ
    chối trường hợp đó TỪ TRƯỚC bằng `file_read_calls`, nên nhánh này không
    bao giờ thực sự chạy tới trong đường sản xuất, nhưng vẫn phải AN TOÀN
    (không ném lỗi lạ) nếu ai đó gọi thẳng hàm này với input khác.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    for table in tree.find_all(exp.Table):
        if table.name:
            continue
        call = table.this
        if not isinstance(call, exp.Func) or _reader_name(call) not in FILE_READ_FUNCTIONS:
            continue
        arg = _path_argument(call)
        if isinstance(arg, exp.Literal) and arg.is_string:
            arg.set("this", resolve(arg.this))
        elif isinstance(arg, exp.Array):
            for item in arg.expressions:
                if isinstance(item, exp.Literal) and item.is_string:
                    item.set("this", resolve(item.this))
    return tree.sql(dialect=dialect)
