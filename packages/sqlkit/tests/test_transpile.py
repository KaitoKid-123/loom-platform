"""Canh `transpile`: phải khẳng định một khác biệt phương ngữ THẬT.

Một `SELECT 1` đi nguyên vẹn qua hai phương ngữ cũng xanh với một hàm trả
nguyên chuỗi đầu vào — không chứng minh được gì. Test dưới dùng khác biệt cú
pháp nối chuỗi (`||` ở DuckDB so với `CONCAT(...)` ở MySQL), một khác biệt
phương ngữ có thật, đã xác nhận bằng thăm dò trực tiếp trên sqlglot 30.15.0.
"""

from loom_sql.transpile import transpile


def test_transpile_rewrites_string_concat_between_real_dialects() -> None:
    result = transpile("SELECT 'a' || 'b'", "duckdb", "mysql")
    assert result == "SELECT CONCAT('a', 'b')"


def test_transpile_rewrites_duckdb_list_literal_to_trino_array() -> None:
    result = transpile("SELECT LIST_VALUE(1, 2, 3)", "duckdb", "trino")
    assert result == "SELECT ARRAY[1, 2, 3]"
