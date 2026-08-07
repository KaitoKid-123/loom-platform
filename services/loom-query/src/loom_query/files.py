"""Đọc file thô trong `Files/` của lakehouse — Task 13, Giai đoạn 2b.

Đây là khe hẹp duy nhất được mở trong lệnh cấm mà `authz.ExternalSourceRejected`
ghi lại (đọc docstring ở đó TRƯỚC khi sửa module này). Ngoài khe hẹp đó, mọi
nguồn đọc dữ liệu không qua catalog vẫn bị chặn NGUYÊN VẸN.

**Hai lớp bảo vệ, khác nhau, KHÔNG lớp nào thay được lớp kia:**

  1. `safe_relative_path` dưới đây — chặn một path THOÁT khỏi `Files/` của
     LAKEHOUSE trong request (`../..`, tuyệt đối, có scheme). Đây là ranh giới
     LAKEHOUSE, chạy TRƯỚC bất kỳ I/O nào (`authz.run_gate`, đồng bộ, xem
     docstring module đó).
  2. Credential do `MinioStsProvider.for_workspace` cấp (`packages/storagekit`)
     — chặn ở tầng MinIO, hẹp theo WORKSPACE, RỘNG HƠN một lakehouse (một
     workspace có thể chứa nhiều lakehouse). Đây là ranh giới WORKSPACE, cấp ở
     `runner.py` khi THẬT SỰ đọc (I/O).

  Một lỗi ở lớp (1) (ví dụ quên gọi `safe_relative_path`, hoặc một cách chuẩn
  hoá không bắt được một dạng mã hoá) rò rỉ dữ liệu của MỘT LAKEHOUSE KHÁC
  TRONG CÙNG WORKSPACE — lớp (2) không chặn được điều đó, vì cả hai lakehouse
  cùng nằm trong phạm vi của MỘT credential workspace. Một lỗi ở lớp (2) (ví
  dụ cấp nhầm credential của workspace khác — xem `runner._install_files_
  secret`) rò rỉ dữ liệu XUYÊN WORKSPACE — lớp (1) không chặn được điều đó, vì
  path tương đối vẫn hợp lệ, chỉ credential/bucket sai. HAI lớp cùng đứng,
  không lớp nào dư — xem báo cáo hoàn tất Task 13 cho phép kiểm chứng minh
  từng lớp riêng.

Module này KHÔNG phụ thuộc FastAPI: `authz.py` (có HTTPException) và
`runner.py` (không có, chạy trong một thread nền không biết gì về request) đều
cần đúng logic validate/resolve này, và cả hai gọi CHUNG một hàm — không phải
hai cài đặt có nguy cơ trôi khỏi nhau (xem `resolve_files_query`).
"""

from __future__ import annotations

import posixpath
import uuid
from dataclasses import dataclass
from urllib.parse import unquote

from loom_sql.deps import file_read_calls, rewrite_file_reads
from loom_storage import prefix_for_lakehouse

# Bố cục đã chốt ở spec Giai đoạn 2 mục 5.1: `Tables/` (Iceberg quản lý) và
# `Files/` (file thô) là hai thứ DUY NHẤT nằm dưới prefix của một lakehouse.
# Case-sensitive CÓ CHỦ ĐÍCH — khớp đúng chữ hoa/thường mà bố cục dùng, không
# đoán khoan dung cho "files/"/"FILES/".
FILES_PREFIX = "Files/"


class UnsafeFilesPath(ValueError):
    """Một path KHÔNG an toàn để đọc qua `read_parquet`/`read_csv`.

    KHÔNG phải `HTTPException` — module này không phụ thuộc FastAPI (xem
    docstring đầu file). `authz.py` bắt exception này và bọc thành
    `InvalidFilesPath` (400, nói rõ lý do) cho client; `runner.py` để nó
    truyền thẳng lên `runner.execute`'s bắt-mọi-lỗi (xem docstring ở đó) như
    một lớp phòng hờ — KHÔNG NÊN xảy ra ở đó vì `run_gate` đã kiểm trước, và
    nếu nó xảy ra thì đó là bằng chứng `run_gate` và `runner` đã trôi khỏi
    nhau.
    """


def _decode_fully(raw: str) -> str:
    """Percent-decode LẶP LẠI tới khi ổn định (tối đa vài vòng).

    Một lớp mã hoá KÉP (`%252e%252e` — `%25` là chính dấu `%`) chỉ lộ ra sau
    HAI lần giải mã; `unquote()` một lần duy nhất bỏ sót nó. Giới hạn 5 vòng
    chỉ để không lặp vô hạn nếu chuỗi chứa `%` không tạo thành mã hợp lệ —
    `unquote` không tự ném lỗi cho `%` sai dạng, nó giữ nguyên ký tự đó, nên
    vòng lặp luôn dừng khi ổn định, thường ở vòng đầu hoặc vòng hai.
    """
    decoded = raw
    for _ in range(5):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            return decoded
        decoded = next_decoded
    return decoded


def safe_relative_path(raw: str) -> str:
    """`raw` (literal người dùng gõ trong `read_parquet('...')`) -> CHÍNH nó
    nếu an toàn, hoặc `UnsafeFilesPath`.

    An toàn nghĩa là: sau khi giải mã percent-encoding và chuẩn hoá `.`/`..`
    kiểu POSIX (tách trên `/`), chuỗi kết quả bắt đầu bằng ĐÚNG literal
    `"Files/"` và còn gì đó sau nó. Đây là phép kiểm DUY NHẤT cần — mọi trường
    hợp TỪ CHỐI trong bảng nghiệm thu (tuyệt đối, thoát prefix, hệ tệp cục bộ,
    mã hoá URL) đều thất bại chính điều kiện này, đã kiểm bằng thực nghiệm cho
    từng trường hợp (xem `tests/test_files.py`):

      - `s3://bat-ky/…`              -> không bắt đầu bằng "Files/"
      - `/etc/passwd`                -> không bắt đầu bằng "Files/"
      - `Files/../../khac/x.parquet` -> chuẩn hoá thành "../khac/x.parquet"
      - `Files/%2e%2e/x.parquet`     -> giải mã thành "Files/../x.parquet",
                                        chuẩn hoá thành "x.parquet"

    Backslash bị chặn RIÊNG, TRƯỚC bước chuẩn hoá — KHÔNG dựa vào điều kiện
    `startswith` ở trên để bắt nó: `posixpath` chỉ tách trên `/`, nên một
    chuỗi dạng `Files/legit\\..\\..\\etc\\passwd` (một segment "legit\\..\\.
    .\\etc\\passwd" duy nhất theo POSIX, vì không có `/` bên trong nó) vẫn bắt
    đầu bằng "Files/" SAU chuẩn hoá — đã kiểm bằng thực nghiệm, `posixpath.
    normpath` không đụng gì tới nó. Trên Linux/S3 hôm nay dấu `\\` không phải
    ký tự đặc biệt nên chuỗi đó KHÔNG thoát thư mục thật — nhưng một cài đặt
    dựa thuần vào `startswith("Files/")` sẽ CHO QUA nó, đúng cái bẫy mà chứng
    minh đỏ 1 của Task 13 nêu tên ("nếu bản cài dùng chuỗi thô thì hai dạng đó
    lọt"). Chặn `\\` vô điều kiện ở đây rẻ, và không phụ thuộc việc một lớp
    khác (proxy, thư viện HTTP) có diễn giải nó khác đi trong tương lai hay
    không.
    """
    if "\\" in raw:
        raise UnsafeFilesPath(f"path must not contain a backslash: {raw!r}")

    decoded = _decode_fully(raw)

    if "\\" in decoded:
        raise UnsafeFilesPath(f"path must not contain a backslash: {raw!r}")

    if "://" in decoded:
        raise UnsafeFilesPath(f"path must not have a scheme: {raw!r}")

    normalized = posixpath.normpath(decoded)
    if normalized == "Files" or not normalized.startswith(FILES_PREFIX):
        raise UnsafeFilesPath(f"path must stay under '{FILES_PREFIX}': {raw!r}")

    return raw


def validate_files_paths(sql: str, dialect: str) -> None:
    """Kiểm AN TOÀN mọi `read_parquet`/`read_csv` trong `sql` — KHÔNG cần biết
    workspace/lakehouse/bucket nào: "path này có thoát khỏi `Files/` hay
    không" là một câu hỏi hoàn toàn độc lập với LAKEHOUSE NÀO đang được hỏi
    (xem `safe_relative_path`) — một `..` thoát khỏi `Files/` thì không an
    toàn bất kể ai gọi.

    `authz.run_gate` gọi ĐÚNG hàm này — nó không có (và không cần) bucket để
    dựng URI đầy đủ, chỉ cần biết CÓ ĐƯỢC hay KHÔNG, TRƯỚC khi trả `202` và
    trước khi chạm S3 (xem module docstring `authz.py`).

    Raises:
        UnsafeFilesPath: một lời gọi không dùng literal chuỗi làm path (biến,
            cột, mảng trộn kiểu...), hoặc dùng một path không nằm trong
            `Files/`.
    """
    for call in file_read_calls(sql, dialect):
        if not call.paths:
            raise UnsafeFilesPath(
                f"{call.function}(...) must be called with a literal string path "
                "(or an array of them) under 'Files/' — a column, expression, or "
                "empty argument cannot be checked for safety"
            )
        for raw in call.paths:
            safe_relative_path(raw)


@dataclass(frozen=True, slots=True)
class FilesQuery:
    """Kết quả kiểm + viết lại MỘT câu SQL cho đường đọc `Files/`.

    `sql` là chuỗi runner đưa thẳng cho DuckDB — GIỮ NGUYÊN bản gốc (cùng
    object, không tái sinh qua sqlglot) nếu câu SQL không có `read_parquet`/
    `read_csv` nào, tránh một vòng parse+tái sinh không cần thiết cho phần lớn
    query (chỉ đọc catalog) — xem `resolve_files_query`.

    `has_file_reads` là cờ để `runner._run_sync` biết có cần `LOAD httpfs` +
    cấp credential hay không (tránh một round trip STS vô ích cho query không
    đọc `Files/` nào).
    """

    sql: str
    has_file_reads: bool


def resolve_files_query(
    sql: str,
    dialect: str,
    *,
    workspace_id: uuid.UUID,
    lakehouse_id: uuid.UUID,
    bucket: str,
) -> FilesQuery:
    """Kiểm an toàn (xem `validate_files_paths`) rồi viết lại mọi path `Files/`
    tương đối trong `sql` thành URI S3 đầy đủ CỦA ĐÚNG `lakehouse_id` — không
    nơi nào khác (LỚP MỘT, xem module docstring).

    Gọi TỪ HAI NƠI, CÙNG một hàm — không phải hai cài đặt: `runner._run_sync`
    gọi nó để lấy SQL thật sự đưa cho DuckDB. `authz.run_gate` KHÔNG gọi hàm
    này (nó không có `bucket` và không cần URI đầy đủ) — nó gọi thẳng
    `validate_files_paths`, phần LÕI mà hàm này cũng gọi lại y hệt ở dưới. Hai
    nơi vì vậy dùng chung ĐÚNG MỘT phép kiểm an toàn, không có nguy cơ trôi
    khỏi nhau.

    Raises:
        UnsafeFilesPath: xem `validate_files_paths`.
    """
    validate_files_paths(sql, dialect)  # phòng hờ — xem docstring trên

    calls = file_read_calls(sql, dialect)
    if not calls:
        return FilesQuery(sql=sql, has_file_reads=False)

    prefix = prefix_for_lakehouse(workspace_id, lakehouse_id)

    def resolve(raw: str) -> str:
        return f"s3://{bucket}/{prefix}{safe_relative_path(raw)}"

    rewritten = rewrite_file_reads(sql, dialect, resolve)
    return FilesQuery(sql=rewritten, has_file_reads=True)
