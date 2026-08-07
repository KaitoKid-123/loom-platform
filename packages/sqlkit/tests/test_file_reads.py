"""Canh `file_read_calls`/`rewrite_file_reads` — nền cho đường đọc `Files/` của
Task 13 (Giai đoạn 2b, `loom_query.files`).

`loom_sql` không biết gì về workspace/lakehouse/S3 (không I/O — xem docstring
đầu `deps.py`): các phép kiểm dưới đây chỉ canh AST, không canh "path này có an
toàn không" (đó là việc của `services/loom-query/tests/test_files.py`).
"""

import pytest

from loom_sql.deps import FileReadCall, file_read_calls, rewrite_file_reads


def test_read_parquet_with_a_single_literal_path() -> None:
    calls = file_read_calls("SELECT * FROM read_parquet('Files/a.parquet')", "duckdb")
    assert calls == [FileReadCall(function="read_parquet", paths=("Files/a.parquet",))]


def test_read_csv_with_a_single_literal_path() -> None:
    calls = file_read_calls("SELECT * FROM read_csv('Files/a.csv')", "duckdb")
    assert calls == [FileReadCall(function="read_csv", paths=("Files/a.csv",))]


def test_read_parquet_with_an_array_of_paths() -> None:
    sql = "SELECT * FROM read_parquet(['Files/a.parquet', 'Files/b.parquet'])"
    calls = file_read_calls(sql, "duckdb")
    assert calls == [
        FileReadCall(function="read_parquet", paths=("Files/a.parquet", "Files/b.parquet"))
    ]


def test_read_csv_with_an_array_of_paths() -> None:
    sql = "SELECT * FROM read_csv(['Files/a.csv', 'Files/b.csv'])"
    calls = file_read_calls(sql, "duckdb")
    assert calls == [FileReadCall(function="read_csv", paths=("Files/a.csv", "Files/b.csv"))]


def test_keyword_options_after_the_path_are_not_mistaken_for_paths() -> None:
    """`hive_partitioning=true`/`delim=','` đứng SAU path trong CÙNG một list
    đối số (đã kiểm bằng thực nghiệm cho `exp.ReadParquet`) — một cài đặt lọc
    theo kiểu `exp.Literal` trên TOÀN BỘ `expressions` thay vì lấy đúng phần tử
    đầu sẽ nuốt luôn `','` của `delim=','` làm một "path" thứ hai."""
    calls = file_read_calls(
        "SELECT * FROM read_parquet('Files/a.parquet', hive_partitioning=true)", "duckdb"
    )
    assert calls == [FileReadCall(function="read_parquet", paths=("Files/a.parquet",))]

    calls = file_read_calls("SELECT * FROM read_csv('Files/a.csv', delim=',')", "duckdb")
    assert calls == [FileReadCall(function="read_csv", paths=("Files/a.csv",))]


def test_a_non_literal_path_argument_yields_no_paths() -> None:
    """Đối số path không phải literal (ở đây: một phép nối chuỗi) — không kiểm
    tĩnh được, nên `paths` phải RỖNG, KHÔNG được đoán hay bỏ qua lời gọi.
    Chỗ gọi (`loom_query.files`) coi rỗng là từ chối."""
    calls = file_read_calls("SELECT * FROM read_parquet('Files/' || col)", "duckdb")
    assert calls == [FileReadCall(function="read_parquet", paths=())]


def test_a_mixed_array_yields_no_paths() -> None:
    """Một phần tử literal, một phần tử biến — TOÀN BỘ mảng không kiểm được,
    không phải 'lấy phần literal, bỏ phần kia' (xem docstring `_string_literals`)."""
    calls = file_read_calls("SELECT * FROM read_parquet(['Files/a.parquet', col])", "duckdb")
    assert calls == [FileReadCall(function="read_parquet", paths=())]


def test_other_known_readers_are_not_included() -> None:
    """`read_csv_auto`/`read_json`/`read_json_auto`/`parquet_scan` KHÔNG nằm
    trong hai hàm mà Task 13 phục vụ — chúng vẫn phải lộ ra ở `Dependencies.
    external` (xem `test_table_deps.py`), nhưng KHÔNG được `file_read_calls`
    thu nhặt. Mở rộng danh sách này là quyết định của một task khác."""
    for sql in [
        "SELECT * FROM read_csv_auto('Files/a.csv')",
        "SELECT * FROM read_json('Files/a.json')",
        "SELECT * FROM read_json_auto('Files/a.json')",
        "SELECT * FROM parquet_scan('Files/a.parquet')",
    ]:
        assert file_read_calls(sql, "duckdb") == [], sql


def test_generate_series_and_range_are_not_included() -> None:
    """`range(10)` không đọc dữ liệu từ đâu cả — không có path nào để kiểm, và
    nó KHÔNG nằm trong hai hàm Task 13 phục vụ dù có bề ngoài (hàm bảng, không
    tên) giống `read_parquet`."""
    assert file_read_calls("SELECT * FROM range(10)", "duckdb") == []
    assert file_read_calls("SELECT * FROM generate_series(1, 10)", "duckdb") == []


def test_an_ordinary_query_has_no_file_read_calls() -> None:
    assert file_read_calls("SELECT * FROM sales.orders", "duckdb") == []


def test_a_call_inside_a_cte_or_join_is_still_found() -> None:
    sql = "WITH f AS (SELECT * FROM read_parquet('Files/a.parquet')) SELECT * FROM f"
    assert file_read_calls(sql, "duckdb") == [
        FileReadCall(function="read_parquet", paths=("Files/a.parquet",))
    ]

    sql = "SELECT * FROM sales.orders o JOIN read_parquet('Files/b.parquet') f ON true"
    assert file_read_calls(sql, "duckdb") == [
        FileReadCall(function="read_parquet", paths=("Files/b.parquet",))
    ]


# --- rewrite_file_reads -------------------------------------------------


def test_rewrite_replaces_a_single_literal_path() -> None:
    sql = "SELECT * FROM read_parquet('Files/a.parquet')"
    out = rewrite_file_reads(sql, "duckdb", lambda raw: f"s3://bucket/{raw}")
    assert out == "SELECT * FROM READ_PARQUET('s3://bucket/Files/a.parquet')"


def test_rewrite_replaces_every_path_in_an_array() -> None:
    sql = "SELECT * FROM read_csv(['Files/a.csv', 'Files/b.csv'])"
    out = rewrite_file_reads(sql, "duckdb", lambda raw: f"s3://bucket/{raw}")
    assert out == "SELECT * FROM READ_CSV(['s3://bucket/Files/a.csv', 's3://bucket/Files/b.csv'])"


def test_rewrite_leaves_keyword_options_untouched() -> None:
    sql = "SELECT * FROM read_csv('Files/a.csv', delim=',')"
    out = rewrite_file_reads(sql, "duckdb", lambda raw: f"s3://bucket/{raw}")
    assert out == "SELECT * FROM READ_CSV('s3://bucket/Files/a.csv', delim = ',')"
    assert "s3://bucket/,'" not in out  # `delim=','` KHÔNG bị coi là một path


def test_rewrite_leaves_the_rest_of_the_query_untouched() -> None:
    sql = "SELECT a.* FROM sales.orders a JOIN read_parquet('Files/x.parquet') b ON a.id = b.id"
    out = rewrite_file_reads(sql, "duckdb", lambda raw: f"s3://bucket/{raw}")
    assert "sales.orders" in out
    assert "s3://bucket/Files/x.parquet" in out


def test_rewrite_propagates_a_rejection_from_resolve() -> None:
    """`resolve` được phép ném lỗi (một đường dẫn không an toàn) — lỗi đó
    không bị `rewrite_file_reads` nuốt."""

    def reject(_raw: str) -> str:
        raise ValueError("unsafe")

    with pytest.raises(ValueError, match="unsafe"):
        rewrite_file_reads("SELECT * FROM read_parquet('Files/a.parquet')", "duckdb", reject)


def test_rewrite_is_a_no_op_when_there_is_nothing_to_rewrite() -> None:
    sql = "SELECT * FROM sales.orders"
    out = rewrite_file_reads(sql, "duckdb", lambda raw: f"s3://bucket/{raw}")
    assert out == "SELECT * FROM sales.orders"
