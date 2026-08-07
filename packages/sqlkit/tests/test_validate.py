"""Canh `validate`: SQL hợp lệ trả `[]`, SQL hỏng phải định vị đúng dòng/cột.

Dòng/cột không phải trang trí: editor Giai đoạn 2c gạch đỏ đúng chỗ dựa vào
đây, và một chuỗi "syntax error" trần trụi bắt người dùng tự dò trong ba mươi
dòng SQL.
"""

from loom_sql.errors import SqlError
from loom_sql.validate import validate


def test_valid_sql_returns_empty_list() -> None:
    """Không có test này thì một `validate` luôn `return []` cũng làm mọi phép
    kiểm lỗi khác xanh — nó không chứng minh gì về việc hàm THỰC SỰ kiểm cú pháp."""
    assert validate("SELECT a, b FROM foo WHERE a > 1", "duckdb") == []


def test_syntax_error_on_line_one_is_reported() -> None:
    errors = validate("SELEC * FROM foo", "duckdb")
    assert len(errors) == 1
    assert isinstance(errors[0], SqlError)
    assert errors[0].line == 1
    assert errors[0].column >= 1
    assert errors[0].message


def test_syntax_error_on_line_three_reports_line_three() -> None:
    """Chỉ phép kiểm này phân biệt được một bản cài trả cứng `line=1`: lỗi ở
    đây nằm ở dòng thứ ba, không phải dòng đầu."""
    sql = "SELECT 1\nFROM foo\nWHERE ((( )"
    errors = validate(sql, "duckdb")
    assert len(errors) == 1
    assert errors[0].line == 3
    assert errors[0].column >= 1


def test_multi_statement_script_validates_every_statement() -> None:
    """Lỗi ở câu lệnh thứ hai (dòng 2) vẫn phải bắt được, không chỉ câu đầu."""
    sql = "SELECT 1;\nSELECT (((;"
    errors = validate(sql, "duckdb")
    assert errors
    assert errors[0].line == 2
