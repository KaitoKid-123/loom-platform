"""Canh `dependencies().reads`/`.writes`/`write_target` — chỗ CTAS gặp RBAC.

Trước Giai đoạn 2c, `dependencies()` coi đích của một CTAS là một bảng cần
ĐỌC, y hệt bảng nguồn — hai hậu quả: `runner` quét một bảng chưa tồn tại
(CTAS luôn hỏng), và `run_gate` chỉ đòi viewer cho GHI (lỗ RBAC). Các bài dưới
đây khoá đúng ba dạng ghi mà spec Giai đoạn 2 quyết định #4 nêu: CTAS,
`CREATE OR REPLACE TABLE ... AS SELECT`, `INSERT INTO ... SELECT`.
"""

from loom_sql.deps import TableRef, dependencies, write_target


def test_ctas_destination_is_a_write_not_a_read() -> None:
    d = dependencies("CREATE TABLE bronze.moi AS SELECT * FROM bronze.nguon", "duckdb")
    assert d.writes == [TableRef("bronze", "moi")]
    assert d.reads == [TableRef("bronze", "nguon")]
    # `tables` (bề mặt cũ, dùng cho lineage) vẫn là HỢP của cả hai.
    assert d.tables == [TableRef("bronze", "moi"), TableRef("bronze", "nguon")]


def test_create_or_replace_ctas_destination_is_a_write() -> None:
    d = dependencies("CREATE OR REPLACE TABLE bronze.moi AS SELECT * FROM bronze.nguon", "duckdb")
    assert d.writes == [TableRef("bronze", "moi")]
    assert d.reads == [TableRef("bronze", "nguon")]


def test_insert_into_select_destination_is_a_write() -> None:
    d = dependencies("INSERT INTO bronze.dest SELECT * FROM bronze.src", "duckdb")
    assert d.writes == [TableRef("bronze", "dest")]
    assert d.reads == [TableRef("bronze", "src")]


def test_a_table_that_is_both_read_and_written_appears_in_both_lists() -> None:
    """`INSERT INTO t SELECT * FROM t` — sqlglot cho ra HAI node `exp.Table`
    khác nhau cùng tên `t`. Bỏ vế `is destination` (so đối tượng) và so bằng
    TÊN thay vào đó sẽ làm bài này đỏ theo một hướng khác: cả hai node cùng bị
    coi là ghi, và `reads` rỗng — sai, vì câu này CÓ đọc `t`."""
    d = dependencies("INSERT INTO t SELECT * FROM t", "duckdb")
    assert d.reads == [TableRef(None, "t")]
    assert d.writes == [TableRef(None, "t")]


def test_plain_ddl_without_as_select_is_still_a_write() -> None:
    """`CREATE TABLE` không kèm `AS SELECT` vẫn TẠO một bảng — vẫn phải đòi
    contributor dù `runner`/`write_target` chưa biết CHẠY nó (không có gì để
    chạy: không `SELECT` nào nhúng bên trong)."""
    d = dependencies("CREATE TABLE ns.t (id INT)", "duckdb")
    assert d.writes == [TableRef("ns", "t")]
    assert d.reads == []


def test_create_view_destination_is_not_a_write() -> None:
    """`CREATE VIEW` không phải `kind == 'TABLE'` — không phải đường ghi mà
    Giai đoạn 2 quyết định #4 mở, và view không tạo dữ liệu trong lakehouse.

    KHÔNG khẳng định gì về `d.reads` ở đây: tên view (`v`) rơi vào `reads` —
    một quirk có TỪ TRƯỚC task này (chưa từng có logic phân biệt đích DDL nào),
    ngoài phạm vi ba dạng ghi mà spec Giai đoạn 2 quyết định #4 liệt kê. Bài
    này chỉ canh ĐÚNG PHẦN việc của task: `CREATE VIEW` không bị đòi
    `contributor` như thể nó là một CTAS."""
    d = dependencies("CREATE VIEW v AS SELECT * FROM ns.raw", "duckdb")
    assert d.writes == []
    assert TableRef("ns", "raw") in d.reads


def test_an_ordinary_select_has_no_writes() -> None:
    """Vế KHẲNG ĐỊNH. Không có nó, một bản cài xếp MỌI bảng vào `writes` cũng
    làm mọi phép trên xanh — và lúc đó SELECT bình thường cũng đòi contributor."""
    d = dependencies("SELECT * FROM sales.orders", "duckdb")
    assert d.writes == []
    assert d.reads == [TableRef("sales", "orders")]


# ------------------------------------------------------------- write_target()


def test_write_target_extracts_the_embedded_select_for_ctas() -> None:
    target = write_target("CREATE TABLE bronze.moi AS SELECT * FROM bronze.nguon", "duckdb")
    assert target is not None
    assert target.ref == TableRef("bronze", "moi")
    assert target.replace is False
    assert "bronze.nguon" in target.select_sql
    assert "SELECT" in target.select_sql.upper()


def test_write_target_marks_replace_for_create_or_replace() -> None:
    target = write_target(
        "CREATE OR REPLACE TABLE bronze.moi AS SELECT * FROM bronze.nguon", "duckdb"
    )
    assert target is not None
    assert target.replace is True


def test_write_target_is_none_for_insert_into_select() -> None:
    """`runner` chưa có đường commit cho `INSERT INTO ... SELECT` (xem
    docstring `write_target`) — `None` ở đây là tín hiệu để runner từ chối rõ
    ràng, không phải lặng lẽ tạo một bảng chỉ tồn tại trong DuckDB `:memory:`."""
    assert write_target("INSERT INTO bronze.dest SELECT * FROM bronze.src", "duckdb") is None


def test_write_target_is_none_for_plain_ddl_without_as_select() -> None:
    assert write_target("CREATE TABLE ns.t (id INT)", "duckdb") is None


def test_write_target_is_none_for_an_ordinary_select() -> None:
    assert write_target("SELECT * FROM sales.orders", "duckdb") is None
