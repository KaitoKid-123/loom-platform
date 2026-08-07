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

    **`reads`/`writes` (Giai đoạn 2c, CTAS)**: `tables` là HỢP của cả hai —
    giữ nguyên hình dạng cũ, nên `table_deps()` (dùng cho lineage Giai đoạn 4)
    không cần đổi gì. `reads` là bảng câu SQL THẬT SỰ đọc (kể cả một bảng vừa
    đọc vừa ghi, ví dụ `INSERT INTO t SELECT * FROM t` — `t` nằm ở CẢ HAI danh
    sách); `writes` là ĐÍCH của `CREATE [OR REPLACE] TABLE ... AS SELECT` hay
    `INSERT INTO ... SELECT`.

    Trước Giai đoạn 2c, `dependencies()` coi đích CTAS là một bảng cần ĐỌC —
    y hệt một bảng nguồn. Hai hậu quả: (1) `runner._run_sync` quét `Lakehouse.
    scan()` cho một bảng CHƯA TỒN TẠI (đích CTAS luôn là bảng mới) trước khi
    câu `CREATE` kịp chạy, nên CTAS luôn hỏng với "table not found"; (2)
    `run_gate` chỉ đòi `item.read` (viewer) cho GHI, trong khi
    `loom_core.roles.ACTION_MATRIX` xếp `item.update` vào `contributor` — một
    viewer tạo được bảng trong lakehouse là một lỗ RBAC, không chỉ thiếu tính
    năng. `reads`/`writes` tách ra ở đây để `authz.run_gate` đòi đúng quyền cho
    từng vế, và `runner` chỉ quét vế `reads`.
    """

    tables: list[TableRef]
    external: list[str]
    reads: list[TableRef]
    writes: list[TableRef]


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


def _destination_table(node: exp.Expression) -> exp.Table | None:
    """Đích ghi của `node` (một `exp.Create` hay `exp.Insert`), `None` nếu
    không đúng hình dạng mong đợi.

    Thăm dò thực nghiệm (sqlglot 30.15.0) trên cả ba dạng ghi — CTAS, `CREATE
    OR REPLACE TABLE ... AS SELECT`, `INSERT INTO ... SELECT`: sqlglot đặt đích
    ở `node.this`, TRỰC TIẾP là `exp.Table` khi câu lệnh KHÔNG kèm danh sách
    cột (`CREATE TABLE t AS SELECT ...`), nhưng BỌC trong `exp.Schema` khi có
    danh sách cột tường minh (`CREATE TABLE t (a, b) AS SELECT ...`, `INSERT
    INTO t (a, b) SELECT ...` — đã kiểm cả hai class `exp.Create`/`exp.Insert`
    cho hình dạng bọc này).
    """
    target = node.this
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _create_table_info(tree: exp.Expression) -> tuple[exp.Table, bool] | None:
    """`(bảng đích, có phải REPLACE hay không)` nếu `tree` là `CREATE [OR
    REPLACE] TABLE ...` (CÓ hay KHÔNG `AS SELECT` — `write_target()` bên dưới
    mới là chỗ đòi phải có `AS SELECT` để CHẠY được); `None` cho `CREATE VIEW`/
    `CREATE SCHEMA`/... (`kind` khác `"TABLE"`, đã kiểm bằng thực nghiệm sqlglot
    trả `kind` là chuỗi thường, ví dụ `"TABLE"`/`"VIEW"`/`"SCHEMA"`) hay không
    tìm thấy đích đúng hình dạng.
    """
    if not (isinstance(tree, exp.Create) and tree.args.get("kind") == "TABLE"):
        return None
    target = _destination_table(tree)
    if target is None:
        return None
    return target, bool(tree.args.get("replace"))


def _write_destination(tree: exp.Expression) -> exp.Table | None:
    """Bảng ĐÍCH ghi của `tree` nếu nó là một trong ba dạng ghi (CTAS, `CREATE
    OR REPLACE TABLE ... AS SELECT`, `INSERT INTO ... SELECT`) — `None` cho
    mọi dạng khác, KỂ CẢ `CREATE TABLE` không có `AS SELECT` (DDL thuần vẫn
    được coi là GHI ở đây — nó tạo một bảng, nên vẫn cần `contributor` — dù
    `runner` chưa biết CHẠY nó, xem `write_target`/báo cáo hoàn tất task).

    So khớp bằng ĐỐI TƯỢNG Python (`is`) khi gọi ở `dependencies()` bên dưới,
    KHÔNG theo tên: `INSERT INTO t SELECT * FROM t` cho ra HAI node `exp.Table`
    khác nhau cùng tên `t` (một đích, một nguồn) — so tên sẽ không phân biệt
    được cái nào là gì.
    """
    create_info = _create_table_info(tree)
    if create_info is not None:
        return create_info[0]
    if isinstance(tree, exp.Insert):
        return _destination_table(tree)
    return None


def dependencies(sql: str, dialect: str) -> Dependencies:
    """Tách phụ thuộc thành bảng catalog (ĐỌC/GHI) và nguồn đọc ngoài catalog.

    `tables`/`reads`/`writes` đã khử trùng lặp, thứ tự ỔN ĐỊNH (sắp theo
    namespace rồi tên) — KHÔNG phải thứ tự xuất hiện. Chỗ gọi so sánh danh
    sách này; một thứ tự đổi theo cách sqlglot duyệt AST giữa các bản sẽ làm
    test đỏ ở nơi không liên quan.

    CTE không phải bảng thật: thăm dò trên sqlglot 30.15.0 xác nhận
    `find_all(exp.Table)` trả về CẢ bí danh CTE lẫn bảng thật nó bọc quanh, nên
    phải loại tên trùng bí danh CTE. Lọc theo TÊN, không theo scope: nếu một CTE
    và một bảng thật trùng tên thì CTE che bảng thật trong phạm vi câu lệnh đó —
    đúng ngữ nghĩa SQL chuẩn. Đích ghi (`destination` bên dưới) được kiểm TRƯỚC
    kiểm CTE — một đích ghi không bao giờ là bí danh CTE (CTE không thể là đích
    của `CREATE`/`INSERT`), nên thứ tự này không bỏ sót gì.
    """
    # `parse_one` khai kiểu trả về là `exp.Expr` (lớp CHA của `exp.Expression`
    # trong stub sqlglot 30.15.0, không phải bí danh của nó) — `cast` ở đây
    # trung thực với thực tế (mọi kết quả `parse_one` LÀ một `exp.Expression`),
    # không phải một cách né mypy: `_write_destination`/`isinstance` bên dưới
    # đều cần API của `exp.Expression`.
    tree = cast(exp.Expression, sqlglot.parse_one(sql, read=dialect))
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    destination = _write_destination(tree)

    reads: set[TableRef] = set()
    writes: set[TableRef] = set()
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

        parts = [p for p in (table.catalog, table.db) if p]
        ref = TableRef(namespace=".".join(parts) if parts else None, name=name)

        if table is destination:
            writes.add(ref)
            continue

        if name in cte_names:
            continue

        reads.add(ref)

    return Dependencies(
        tables=sorted(reads | writes, key=lambda r: (r.namespace or "", r.name)),
        external=sorted(external),
        reads=sorted(reads, key=lambda r: (r.namespace or "", r.name)),
        writes=sorted(writes, key=lambda r: (r.namespace or "", r.name)),
    )


def table_deps(sql: str, dialect: str) -> list[TableRef]:
    """Chỉ các bảng catalog.

    Giữ lại vì nó là bề mặt hẹp, tiện cho việc dựng lineage ở Giai đoạn 4. **Chỗ
    kiểm quyền KHÔNG được dùng hàm này** — nó bỏ qua `external`, và bỏ qua
    `external` là bỏ qua đúng những đường đọc dữ liệu không đi qua catalog.
    """
    return dependencies(sql, dialect).tables


# --- Giai đoạn 2c: đường CTAS thật (runner cần biết CHẠY gì, không chỉ AI ------
#     được phép đọc/ghi bảng nào) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteTarget:
    """Đích ghi CÓ THỂ CHẠY được của một câu SQL, cùng câu `SELECT` nhúng bên
    trong nó — cái mà `runner` cần để tự chạy phần đọc trên DuckDB rồi COMMIT
    kết quả ra Iceberg qua `Lakehouse.create_from()` (xem
    `loom_query.runner`), THAY VÌ đưa cả câu `CREATE ... AS SELECT` cho DuckDB
    chạy thẳng — `CREATE TABLE` của DuckDB tạo bảng trong catalog `:memory:`
    riêng của chính connection đó, không phải trong Iceberg, và bảng đó biến
    mất khi connection đóng.

    `select_sql` đã tái sinh qua chính dialect gốc (`sql.dialect=dialect`),
    giữ NGUYÊN tên bảng/alias người dùng đã viết — runner chạy nó trên CÙNG
    connection đã đăng ký view cho các bảng ĐỌC (`Dependencies.reads`), nên
    tên phải khớp nguyên vẹn.
    """

    ref: TableRef
    replace: bool
    select_sql: str


def write_target(sql: str, dialect: str) -> WriteTarget | None:
    """`sql` có phải CTAS CHẠY ĐƯỢC — `CREATE [OR REPLACE] TABLE ... AS
    SELECT ...` — hay không; `None` cho MỌI dạng khác, kể cả hai dạng ghi còn
    lại mà `dependencies()` ở trên VẪN xếp vào `writes` (nên VẪN đòi
    `contributor`) nhưng `runner` CHƯA có đường commit thật:

    - `INSERT INTO ... SELECT` — đích thường đã tồn tại, ghi thêm dòng
      (`Lakehouse.append`) là một task khác, chưa tới lượt ở đây.
    - `CREATE TABLE ...` KHÔNG `AS SELECT` (DDL thuần, không `expression`) —
      không có dữ liệu nào để chạy, không có gì để commit.

    Một PHÉP DUYỆT RIÊNG, KHÔNG tái dùng cây của `dependencies()` — cùng lý do
    đã ghi ở `file_read_calls`: hai hàm trả lời hai câu hỏi khác nhau ("bảng
    nào bị kiểm quyền" so với "có gì để runner CHẠY"), và `runner` gọi hàm này
    trên `sql` ĐÃ qua `resolve_files_query` (path `Files/` đã được viết lại),
    khác với `sql` gốc mà `run_gate` đưa cho `dependencies()` — dùng chung một
    cây đã phân tích của bản SQL sai (gốc so với đã viết lại) sẽ chạy nhầm
    path.
    """
    tree = cast(exp.Expression, sqlglot.parse_one(sql, read=dialect))  # xem `dependencies()`
    create_info = _create_table_info(tree)
    if create_info is None:
        return None
    target, replace = create_info
    select = tree.args.get("expression")
    if select is None:
        return None
    parts = [p for p in (target.catalog, target.db) if p]
    ref = TableRef(namespace=".".join(parts) if parts else None, name=target.name)
    return WriteTarget(ref=ref, replace=replace, select_sql=select.sql(dialect=dialect))


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
