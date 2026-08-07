"""Canh `table_deps` — chỗ RBAC gặp SQL.

Mỗi test dưới đây khoá một đường bỏ sót THẬT: xem báo cáo Task 4 để biết cách
mỗi test được chứng minh ĐỎ (phá cài đặt theo đúng kiểu bỏ sót mà tên test nói,
xác nhận đỏ, rồi phục hồi).
"""

from loom_sql.deps import TableRef, table_deps


def test_join_returns_every_table_not_just_the_first() -> None:
    sql = "SELECT * FROM a JOIN b ON a.id = b.id JOIN c ON b.id = c.id"
    assert table_deps(sql, "duckdb") == [
        TableRef(None, "a"),
        TableRef(None, "b"),
        TableRef(None, "c"),
    ]


def test_cte_alias_is_not_a_table() -> None:
    sql = "WITH x AS (SELECT * FROM real_table) SELECT * FROM x"
    assert table_deps(sql, "duckdb") == [TableRef(None, "real_table")]


def test_nested_subquery_finds_table_at_every_level() -> None:
    sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM inner_t) t1) t2"
    assert table_deps(sql, "duckdb") == [TableRef(None, "inner_t")]


def test_union_returns_tables_from_both_sides() -> None:
    sql = "SELECT * FROM t1 UNION SELECT * FROM t2"
    assert table_deps(sql, "duckdb") == [TableRef(None, "t1"), TableRef(None, "t2")]


def test_alias_resolves_to_the_real_table_name() -> None:
    sql = "SELECT * FROM real_table AS r"
    assert table_deps(sql, "duckdb") == [TableRef(None, "real_table")]


def test_insert_into_select_returns_both_destination_and_source() -> None:
    sql = "INSERT INTO dest SELECT * FROM src"
    assert table_deps(sql, "duckdb") == [TableRef(None, "dest"), TableRef(None, "src")]


def test_cte_shadows_a_real_table_of_the_same_name() -> None:
    """Chỗ nguy hiểm nhất trong cả bảy: trả nhầm `sales` nghĩa là kiểm quyền
    một bảng câu SQL không hề đọc, và người dùng bị từ chối oan. Ngược lại nếu
    lọc CTE quá tay và bỏ luôn `raw`, một bảng thật thoát kiểm quyền — tệ hơn
    nhiều so với từ chối oan."""
    sql = "WITH sales AS (SELECT * FROM raw) SELECT * FROM sales"
    assert table_deps(sql, "duckdb") == [TableRef(None, "raw")]


def test_result_is_deduplicated() -> None:
    sql = "SELECT * FROM b JOIN a ON 1 = 1 JOIN b ON 1 = 1"
    assert table_deps(sql, "duckdb") == [TableRef(None, "a"), TableRef(None, "b")]


def test_namespace_is_captured_when_the_sql_states_it() -> None:
    sql = "SELECT * FROM myschema.mytable"
    assert table_deps(sql, "duckdb") == [TableRef("myschema", "mytable")]


def test_order_is_sorted_not_appearance_order() -> None:
    """Thứ tự phải ổn định theo sắp xếp, không phải thứ tự sqlglot duyệt AST —
    nêu rõ bằng cách liệt kê bảng theo thứ tự NGƯỢC bảng chữ cái trong SQL."""
    sql = "SELECT * FROM zebra JOIN apple ON 1 = 1"
    assert table_deps(sql, "duckdb") == [TableRef(None, "apple"), TableRef(None, "zebra")]
