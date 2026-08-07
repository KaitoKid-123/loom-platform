"""Canh `loom_query.files` — LỚP MỘT của đường đọc `Files/` (Task 13, Giai
đoạn 2b): path nào được coi là an toàn để `read_parquet`/`read_csv` đọc.

KHÔNG cần Docker/MinIO — `safe_relative_path`/`validate_files_paths`/
`resolve_files_query` thuần chuỗi + AST, không I/O (xem docstring `files.py`).
Vế "credential có thật sự bị MinIO chặn hay không" (LỚP HAI) nằm ở
`tests/integration/test_query_files_read.py`, cần MinIO thật.
"""

from __future__ import annotations

import uuid

import pytest

from loom_query.files import (
    FilesQuery,
    UnsafeFilesPath,
    resolve_files_query,
    safe_relative_path,
    validate_files_paths,
)

# --- safe_relative_path — bảng nghiệm thu của Task 13 -----------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Files/thang-01/a.parquet",
        "Files/thang-01/*.parquet",
        "Files/a.csv",
        "Files/nested/deep/path/x.parquet",
    ],
)
def test_relative_paths_under_files_are_allowed(raw: str) -> None:
    assert safe_relative_path(raw) == raw


def test_an_absolute_local_filesystem_path_is_rejected() -> None:
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("/etc/passwd")


def test_an_absolute_s3_uri_is_rejected() -> None:
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("s3://bat-ky/bi-mat.parquet")


def test_a_different_scheme_is_also_rejected() -> None:
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("gcs://bat-ky/bi-mat.parquet")


def test_escaping_the_files_prefix_with_dotdot_is_rejected() -> None:
    """Chứng minh đỏ 1 (nêu nguyên văn trong spec Task 13): bỏ bước chuẩn hoá
    `posixpath.normpath` trong `safe_relative_path` (ví dụ thay bằng một kiểm
    tra chuỗi thô `"../" not in raw`) làm phép kiểm NÀY vẫn xanh (nó không
    chứa `"../"`  ở gần đủ literal để một kiểm tra ngây thơ khác bắt được theo
    cách khác — xem hai test mã hoá/backslash bên dưới cho đúng lỗ mà một cách
    chuẩn hoá THIẾU sót để lọt) nhưng làm hai test đó ĐỎ."""
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files/../../khac/x.parquet")


def test_escaping_via_url_encoded_dotdot_is_rejected() -> None:
    """`%2e%2e` là `..` mã hoá URL. Một cài đặt chuẩn hoá CHUỖI THÔ (không gọi
    `unquote` trước `posixpath.normpath`) sẽ thấy "Files/%2e%2e/x.parquet" bắt
    đầu bằng "Files/" và không có ".." theo nghĩa đen — và cho qua SAI."""
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files/%2e%2e/%2e%2e/khac/x.parquet")


def test_escaping_via_double_url_encoded_dotdot_is_rejected() -> None:
    """`%252e` giải mã một lần ra `%2e`, phải giải mã LẦN THỨ HAI mới ra `.`.
    Một `unquote()` gọi đúng MỘT lần bỏ sót dạng này."""
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files/%252e%252e/khac/x.parquet")


def test_a_bare_dotdot_after_files_is_rejected() -> None:
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files/../x.parquet")


def test_backslash_traversal_after_a_valid_prefix_is_rejected() -> None:
    """Chứng minh đỏ 1, phần "dấu \\" nêu trong spec Task 13: `posixpath` chỉ
    tách trên `/`, nên "Files/legit\\..\\..\\etc\\passwd" là MỘT segment duy
    nhất theo POSIX (không có `/` bên trong) và `posixpath.normpath` không đụng
    gì tới nó — chuỗi kết quả VẪN bắt đầu bằng "Files/". Một cài đặt chỉ kiểm
    `normalized.startswith("Files/")` mà KHÔNG có bước chặn `\\` riêng sẽ CHO
    QUA chuỗi này. Xoá bước chặn `\\` khỏi `safe_relative_path` để chứng minh
    đỏ: phép kiểm này (và chỉ đúng phép kiểm này trong nhóm backslash) phải
    đỏ, còn `test_a_bare_backslash_path_is_rejected` bên dưới vẫn xanh vì lý
    do khác (nó rớt ở chính điều kiện `startswith`)."""
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files/legit\\..\\..\\etc\\passwd")


def test_a_bare_backslash_path_is_rejected() -> None:
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files\\..\\..\\x.parquet")


def test_lowercase_files_prefix_is_rejected() -> None:
    """Bố cục spec dùng ĐÚNG chữ hoa "Files/" — case-sensitive có chủ đích,
    không đoán khoan dung."""
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("files/a.parquet")


def test_a_bare_files_with_nothing_after_it_is_rejected() -> None:
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Files/")


def test_a_path_that_merely_starts_with_the_word_files_is_rejected() -> None:
    """ "Filesystem/x" bắt đầu bằng năm ký tự "Files" nhưng KHÔNG bắt đầu bằng
    literal "Files/" — phải rớt, không được khớp mờ theo tiền tố chuỗi con."""
    with pytest.raises(UnsafeFilesPath):
        safe_relative_path("Filesystem/x.parquet")


# --- validate_files_paths / resolve_files_query -----------------------------


def test_validate_accepts_a_query_with_only_safe_reads() -> None:
    validate_files_paths("SELECT * FROM read_parquet('Files/a.parquet')", "duckdb")  # không ném


def test_validate_rejects_a_query_with_an_unsafe_read() -> None:
    with pytest.raises(UnsafeFilesPath):
        validate_files_paths("SELECT * FROM read_parquet('s3://bat-ky/x.parquet')", "duckdb")


def test_validate_rejects_a_non_literal_path_argument() -> None:
    with pytest.raises(UnsafeFilesPath):
        validate_files_paths("SELECT * FROM read_parquet(some_column)", "duckdb")


def test_validate_ignores_queries_with_no_file_reads() -> None:
    validate_files_paths("SELECT * FROM sales.orders", "duckdb")  # không ném


def test_resolve_returns_the_original_sql_unchanged_when_there_is_nothing_to_resolve() -> None:
    sql = "SELECT * FROM sales.orders"
    result = resolve_files_query(
        sql,
        "duckdb",
        workspace_id=uuid.uuid4(),
        lakehouse_id=uuid.uuid4(),
        bucket="loom-local",
    )
    assert result == FilesQuery(sql=sql, has_file_reads=False)


def test_resolve_rewrites_a_relative_path_into_a_full_s3_uri_for_this_lakehouse() -> None:
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    result = resolve_files_query(
        "SELECT * FROM read_parquet('Files/thang-01/a.parquet')",
        "duckdb",
        workspace_id=workspace_id,
        lakehouse_id=lakehouse_id,
        bucket="loom-local",
    )
    assert result.has_file_reads is True
    expected_uri = (
        f"s3://loom-local/workspaces/{workspace_id}/lakehouses/{lakehouse_id}/"
        "Files/thang-01/a.parquet"
    )
    assert expected_uri in result.sql


def test_resolve_never_lets_a_path_reach_a_different_lakehouse() -> None:
    """Không có cú pháp nào trong một path TƯƠNG ĐỐI cho phép trỏ sang
    lakehouse khác — `resolve_files_query` LUÔN nối prefix của ĐÚNG
    `lakehouse_id` được truyền vào, không đọc gì khác từ chính câu SQL để
    quyết định lakehouse. Phép kiểm này khẳng định trực tiếp điều đó: hai lần
    gọi cùng SQL, khác `lakehouse_id`, phải ra hai URI khác nhau, MỖI URI mang
    đúng lakehouse đã truyền — không có cách nào để SQL "ghi đè" nó."""
    workspace_id = uuid.uuid4()
    lakehouse_a = uuid.uuid4()
    lakehouse_b = uuid.uuid4()
    sql = "SELECT * FROM read_parquet('Files/a.parquet')"

    result_a = resolve_files_query(
        sql, "duckdb", workspace_id=workspace_id, lakehouse_id=lakehouse_a, bucket="b"
    )
    result_b = resolve_files_query(
        sql, "duckdb", workspace_id=workspace_id, lakehouse_id=lakehouse_b, bucket="b"
    )
    assert str(lakehouse_a) in result_a.sql
    assert str(lakehouse_b) not in result_a.sql
    assert str(lakehouse_b) in result_b.sql
    assert str(lakehouse_a) not in result_b.sql


def test_resolve_rejects_an_unsafe_path_without_building_a_uri() -> None:
    with pytest.raises(UnsafeFilesPath):
        resolve_files_query(
            "SELECT * FROM read_parquet('Files/../../khac/x.parquet')",
            "duckdb",
            workspace_id=uuid.uuid4(),
            lakehouse_id=uuid.uuid4(),
            bucket="loom-local",
        )
