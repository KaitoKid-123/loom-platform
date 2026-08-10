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
    """Lỗi ở câu lệnh thứ hai (dòng 2) vẫn phải bắt được, không chỉ câu đầu.

    Nhiều câu lệnh bị TỪ CHỐI (xem phép kiểm bên dưới), nhưng lỗi cú pháp phải
    thắng trước: nó chỉ đúng chỗ hỏng, còn "chỉ một câu lệnh" thì không.
    """
    sql = "SELECT 1;\nSELECT (((;"
    errors = validate(sql, "duckdb")
    assert errors
    assert errors[0].line == 2


def test_two_valid_statements_are_rejected() -> None:
    """Chặn nhiều câu lệnh là một hàng rào QUYỀN, không phải chuyện phong cách.

    `dependencies()` dùng `parse_one`, và với nhiều câu lệnh sqlglot trả về một
    `exp.Block` mà `_write_destination` không nhận ra — nên đích GHI bị xếp
    thành bảng ĐỌC, và `run_gate` chỉ đòi viewer cho một câu lệnh ghi.
    """
    errors = validate("SELECT 1; CREATE TABLE ns.t AS SELECT 1", "duckdb")
    assert len(errors) == 1
    assert "MỘT câu lệnh" in errors[0].message


def test_a_trailing_semicolon_is_not_a_second_statement() -> None:
    """Nếu phép chặn ở trên đếm nhầm, mọi câu SQL người dùng gõ kết thúc bằng
    dấu chấm phẩy — tức gần như mọi câu — sẽ bị từ chối."""
    assert validate("SELECT 1;", "duckdb") == []
    assert validate("SELECT 1 ;  ", "duckdb") == []
