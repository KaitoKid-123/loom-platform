"""Hàng rào DÙNG CHUNG cho mọi script ĐO chạm vào Aiven.

Đây không phải một tiện ích cho gọn. Nó tồn tại vì HAI SỰ CỐ ĐÃ XẢY RA THẬT trên
chính service Aiven của chủ dự án, trong lúc control plane của Loom đang sống
trên đó — và cả hai đều bắt nguồn từ một phép ĐO, không phải từ mã production.

**Sự cố 1 — hết đĩa, cả service chuyển CHỈ-ĐỌC.** `measure_ingest_path.py` nạp
một bảng bench vào Aiven. Hàng rào của bản đầu kiểm dung lượng ĐÚNG MỘT LẦN
trước khi nạp, ước lượng 330 byte/dòng (đo lại: 350), và không tính WAL của
chính lần nạp. Ở dòng thứ 1.000.000 Aiven chuyển CẢ service sang chỉ-đọc; ngay
cả `DROP SCHEMA` dọn dẹp cũng bị từ chối, tức phép đo tự nhốt mình. Trong vài
phút đó control plane của chủ dự án cũng không ghi được.

**Sự cố 2 — hết connection slot.** Giết một pod đang đọc giữa chừng để lại các
backend chưa đóng. Service này có `max_connections=20` và nó KHÔNG chỉ phục vụ
Loom — đo được ngày 2026-08-14: lakekeeper 7, `bi_portal` (một ứng dụng KHÁC của
chủ dự án, application_name "PostgreSQL JDBC Driver") 5, database `loom` 4. Tức
phần còn trống cho một pod nạp mới gần bằng KHÔNG ngay cả khi không ai làm gì
sai. Một phép đo mở thêm vài connection là đủ để `make smoke` trượt ở đúng ô nạp.

## Vì sao MỘT chỗ, không chép vào từng script

Bản trước có hai hàng rào song song: `probe_read_path_cost.py` mở chỉ-đọc và đo
đĩa TRƯỚC/SAU CẢ LẦN CHẠY; `measure_ingest_path.py` đo đĩa sau MỖI KHỐI nhưng mở
connection GHI ĐƯỢC. Mỗi bản giữ đúng một nửa bài học của sự cố kia. Đó chính là
cách một hàng rào được chép sẽ trôi: không ai sai, nhưng không chỗ nào đủ.

Ở đây chỉ có một định nghĩa, và `packages/connectorkit/tests/test_aiven_
measurement_guard.py` canh rằng không script nào dựng DSN Aiven bằng tay nữa.

## Ba tính chất mà hàng rào này thi hành

1. **Chỉ-đọc, ở tầng server.** `-c default_transaction_read_only=on` đi vào DSN,
   nên Postgres TỪ CHỐI mọi `INSERT`/`CREATE`/`COPY FROM` trên connection này —
   kể cả khi một thay đổi sau này vô tình thêm một câu như thế. Docstring không
   chặn được gì; tham số server thì có.
2. **Kiểm dung lượng TRƯỚC VÀ SAU MỖI KHỐI**, không một lần lúc đầu. Đĩa đi lên
   TRONG LÚC chạy, và thứ đẩy service qua mép không chỉ là dữ liệu mà cả WAL của
   chính tải đang chạy — đó là chỗ bản đầu của `check_source_disk` đã vỡ.
3. **Không sinh dữ liệu vào Aiven.** `generate_series` sinh dòng ở phía server,
   không chạm một page nào trên đĩa và không sinh một byte WAL nào. Mọi phép đo
   cần "N dòng" phải lấy chúng theo đường đó.

Tính chất 3 có MỘT hệ quả phải nói ra chứ không giấu: `generate_series` tốn CPU
server, còn đọc một bảng đã lưu thì không. Hai thứ đó KHÔNG bằng nhau, và ĐO 4
đã lượng hoá chênh lệch (ô connector trên `generate_series` 6,68 MB/s so với
7,30 MB/s mà ĐO 3 đo trên bảng thật). Nên số đo bằng `generate_series` là số của
một hình dạng tải HƠI KHÁC, và báo cáo nào dùng nó phải nói thế.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import psycopg

# Chuỗi này là CẢ hàng rào chỉ-đọc. Nó nằm ở đúng một chỗ trong repo, và test
# `test_only_the_guard_declares_read_only` canh rằng không script nào viết lại
# nó — một bản chép thứ hai là một chỗ để quên `on` mà không ai thấy.
READ_ONLY_OPTION: Final[str] = "-c default_transaction_read_only=on"

# 15 phút. Không phải một con số tròn cho đẹp: một ô đo 500.000 dòng qua đường
# truyền tới Aiven mất ~15-25 giây, nên 900 giây là hai bậc độ lớn dư — nó ở đây
# để một câu treo KHÔNG giữ một backend vô hạn trên service 20 slot, chứ không
# để cắt một phép đo bình thường.
STATEMENT_TIMEOUT_MS: Final[int] = 900_000

_CREDENTIAL_KEYS: Final[tuple[str, ...]] = ("host", "port", "dbname", "username", "password")


class _Cursor(Protocol):
    """Chỉ phần API mà hàng rào dùng — để test bơm được một cursor giả vào.

    Hẹp có chủ ý: nếu Protocol này rộng bằng `psycopg.Cursor` thì test phải dựng
    một cursor thật, và khi đó phép canh hàng rào lại cần đúng cái service mà nó
    sinh ra để bảo vệ.
    """

    def execute(self, query: str, params: Any = ..., /) -> Any: ...
    def fetchone(self) -> Any: ...


# ───────────────────────── nguồn credential ─────────────────────────
#
# Ba HÌNH DẠNG của cùng một bộ khoá, vì ba chỗ chạy khác nhau. Giá trị KHÔNG BAO
# GIỜ được in ra, không đi qua dòng lệnh, không vào `progress.json`.


def read_env_file(path: Path) -> dict[str, str]:
    """`deploy/local/aiven.env` — bản chạy trên HOST (file gitignore, khoá THẬT)."""
    if not path.exists():
        raise SystemExit(
            f"Thiếu {path} — copy từ aiven.env.example rồi điền (xem make infra-local-secret)."
        )
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def read_secret_dir(path: Path) -> dict[str, str]:
    """Thư mục Secret đã mount — bản chạy TRONG CỤM.

    kubelet chiếu một Secret thành MỘT FILE MỖI KHOÁ (`/aiven/username`, ...)
    chứ không phải một file `key=value`. Đọc THẲNG từ file là chủ ý: giá trị
    không đi qua dòng lệnh (`ps` trong pod không thấy), không qua biến môi
    trường (`kubectl describe pod` không thấy), và không vào log.

    `.rstrip("\\n")` chứ không `.strip()`: mật khẩu có thể mở/kết thúc bằng
    khoảng trắng hợp lệ, và `--from-env-file` của kubectl lưu giá trị NGUYÊN VĂN
    — chỉ bỏ đúng ký tự xuống dòng mà trình soạn thảo thêm vào.
    """
    if not path.is_dir():
        raise SystemExit(f"--aiven-secret-dir {path} không phải thư mục (Secret đã mount chưa?)")
    values: dict[str, str] = {}
    for key in _CREDENTIAL_KEYS:
        item = path / key
        if item.is_file():
            values[key] = item.read_text().rstrip("\n")
    return values


def read_environ(prefix: str = "BENCH_PG_") -> dict[str, str]:
    """Biến môi trường `BENCH_PG_*` — bản chạy TRONG CỤM của `measure_ingest_path`.

    Hình dạng thứ ba vì Job của phép đo đó lấy từng khoá qua `secretKeyRef` chứ
    không mount cả Secret. Giữ nguyên hình dạng đó thay vì bắt nó đổi theo hai
    bản kia: đổi hình dạng Secret trong Makefile là một thay đổi CÓ RỦI RO ở chỗ
    không liên quan gì tới hàng rào này.
    """
    mapping = {
        "host": f"{prefix}HOST",
        "port": f"{prefix}PORT",
        "dbname": f"{prefix}DBNAME",
        "username": f"{prefix}USER",
        "password": f"{prefix}PASSWORD",
    }
    return {key: os.environ[name] for key, name in mapping.items() if name in os.environ}


# ───────────────────────────── DSN ─────────────────────────────


def build_read_only_dsn(
    credentials: dict[str, str],
    *,
    ca_path: Path | None = None,
    source: str = "credential",
) -> str:
    """DSN tới Aiven, MỞ Ở CHẾ ĐỘ CHỈ-ĐỌC. Đây là đường DUY NHẤT dựng DSN Aiven.

    Không có tham số nào tắt được `READ_ONLY_OPTION`, và đó là cả điểm: một cờ
    `read_only=False` sẽ được ai đó truyền vào lúc 2 giờ sáng để "chỉ tạo một
    bảng bench thôi", và đó chính xác là câu chuyện của Sự cố 1.

    `sslmode=verify-full` khi có CA: xác thực CẢ hostname. Không có CA thì
    `require` — mã hoá nhưng KHÔNG xác thực danh tính server. Bản trong cụm chạy
    ở nhánh sau, nên nói thẳng ra đây thay vì để một mặc định im lặng quyết định.

    Chuỗi trả về CÓ MẬT KHẨU — không log nó, không in nó, không đưa vào JSON.
    """
    missing = [key for key in _CREDENTIAL_KEYS if not credentials.get(key)]
    if missing:
        raise SystemExit(f"{source} thiếu khoá: {', '.join(missing)}")

    if ca_path is not None:
        if not ca_path.exists():
            raise SystemExit(f"Thiếu {ca_path} — tải CA từ console Aiven (xem aiven.env.example).")
        tls = f"sslmode=verify-full sslrootcert={ca_path}"
    else:
        tls = "sslmode=require"

    options = f"{READ_ONLY_OPTION} -c statement_timeout={STATEMENT_TIMEOUT_MS}"
    return (
        f"host={credentials['host']} port={credentials['port']} "
        f"dbname={credentials['dbname']} user={credentials['username']} "
        f"password={credentials['password']} {tls} options='{options}'"
    )


def aiven_dsn(env_path: Path, ca_path: Path, secret_dir: Path | None = None) -> str:
    """Đường tiện cho các script: chọn hình dạng credential rồi dựng DSN chỉ-đọc."""
    if secret_dir is not None:
        return build_read_only_dsn(
            read_secret_dir(secret_dir), ca_path=ca_path, source=str(secret_dir)
        )
    return build_read_only_dsn(read_env_file(env_path), ca_path=ca_path, source=str(env_path))


def dsn_from_environ(prefix: str = "BENCH_PG_") -> str:
    """DSN chỉ-đọc từ `BENCH_PG_*`. CA không có trong Job đó, nên `sslmode=require`."""
    return build_read_only_dsn(read_environ(prefix), ca_path=None, source=f"biến {prefix}*")


# ─────────────────────── mở connection ───────────────────────


def verify_read_only(conn: psycopg.Connection[Any]) -> list[str]:
    """CỐ Ý thử GHI, để hàng rào chỉ-đọc là BẰNG CHỨNG chứ không phải lời hứa.

    `SHOW default_transaction_read_only` chỉ nói tham số ĐƯỢC ĐẶT; nó không
    chứng minh server THI HÀNH nó. Hai câu dưới đây chứng minh: cả bảng TẠM
    (thứ nhiều người tưởng là ngoại lệ vì nó không sinh WAL cho bảng thường)
    lẫn bảng thường đều phải bị từ chối.

    Nếu một câu nào đó THÀNH CÔNG thì giả định nền của cả phép đo đã sai và
    script DỪNG ngay — `rollback` ở `finally` gỡ lại thứ vừa tạo (connection
    không autocommit), rồi thoát trước khi bất cứ phép đo nào chạy.
    """
    attempts = (
        ("CREATE TEMP TABLE probe_readonly_check (x int)", "CREATE TEMP TABLE"),
        ("CREATE TABLE probe_readonly_check_perm (x int)", "CREATE TABLE"),
    )
    lines: list[str] = []
    for statement, label in attempts:
        rejected = False
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
        except psycopg.errors.ReadOnlySqlTransaction as exc:
            rejected = True
            detail = str(exc).strip().splitlines()[0]
            lines.append(f"TỪ CHỐI (đúng như mong đợi): {label} -> {type(exc).__name__}: {detail}")
        finally:
            conn.rollback()
        if not rejected:
            raise SystemExit(
                f"HÀNG RÀO CHỈ-ĐỌC HỎNG: server CHẤP NHẬN `{label}`. Đã rollback, "
                "nhưng KHÔNG chạy phép đo nào nữa — sửa DSN trước."
            )
    return lines


def connect_read_only(
    dsn: str, *, connect_timeout: int = 20, verify: bool = False
) -> psycopg.Connection[Any]:
    """Mở connection và ĐỌC LẠI tham số chỉ-đọc từ chính server.

    Không tin chuỗi DSN: một `options` sai chính tả vẫn cho connect thành công
    và im lặng bỏ qua tham số. Chỉ câu `SHOW` mới nói server thực sự đang ở chế
    độ nào, nên nó chạy trước khi connection được trả cho người gọi.

    `verify=True` chạy thêm `verify_read_only` — phép thử GHI thật. Nó tốn hai
    lượt đi-về nên không bật mặc định, nhưng bản chạy trong cụm thì nên bật.
    """
    conn = psycopg.connect(dsn, connect_timeout=connect_timeout)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            row = cur.fetchone()
        setting = str(row[0]).lower() if row else "?"
        if setting not in ("on", "true"):
            raise SystemExit(
                f"TỪ CHỐI CHẠY: connection Aiven KHÔNG ở chế độ chỉ-đọc "
                f"(SHOW default_transaction_read_only = {setting!r})."
            )
        conn.rollback()
        if verify:
            verify_read_only(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def read_only_connection(
    dsn: str, *, connect_timeout: int = 20, verify: bool = False
) -> Iterator[psycopg.Connection[Any]]:
    """`connect_read_only` nhưng ĐÓNG chắc chắn, kể cả khi phép đo ném lỗi.

    Sự cố 2 là về connection không được trả lại. `try/finally` không cứu được
    một pod bị `SIGKILL`, và câu này không giả vờ ngược lại — nó chỉ đóng chặt
    con đường mà mã Python CÓ THỂ đóng.
    """
    conn = connect_read_only(dsn, connect_timeout=connect_timeout, verify=verify)
    try:
        yield conn
    finally:
        conn.close()


# ───────────────────── dung lượng & connection slot ─────────────────────


def total_database_bytes(cur: _Cursor) -> int:
    """Tổng MỌI database, không riêng database bench.

    Đĩa là của SERVICE: Lakekeeper ghi vào cùng volume đó ở mỗi commit catalog,
    tức là trong suốt phép đo. Đo riêng một database sẽ bỏ sót đúng phần đang
    tăng.
    """
    cur.execute("SELECT COALESCE(sum(pg_database_size(datname)), 0)::bigint FROM pg_database")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def connection_slots(cur: _Cursor) -> tuple[int, int]:
    """(đang dùng, tối đa) — Sự cố 2, đo được thay vì đoán."""
    cur.execute("SELECT count(*)::int FROM pg_stat_activity")
    used_row = cur.fetchone()
    cur.execute("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
    max_row = cur.fetchone()
    return (int(used_row[0]) if used_row else 0, int(max_row[0]) if max_row else 0)


@dataclass
class StorageHeadroom:
    """Kiểm dung lượng còn lại TRƯỚC VÀ SAU MỖI KHỐI, không một lần lúc đầu.

    `ceiling_bytes` là một trần TỰ ĐẶT và nó phải đứng DƯỚI mốc ĐÃ QUAN SÁT được
    là nguy hiểm (432 MB tổng mọi database, lúc Aiven lật service sang chỉ-đọc),
    không đứng ở một con số tròn nghe hợp lý. Không có API nào trong SQL cho
    biết dung lượng gói, nên trần này là phán đoán của con người dựa trên một số
    ĐO — và nó được ghi ra đây để phán đoán đó kiểm chứng được.

    `first_seen`/`last_seen` giữ lại hai đầu để báo cáo in được CHÊNH LỆCH thật
    của cả lần chạy: với một phép đo chỉ-đọc dùng `generate_series`, chênh lệch
    đó phải bằng 0, và đó là BẰNG CHỨNG "không ghi gì vào Aiven" chứ không phải
    một lời hứa trong docstring.
    """

    ceiling_bytes: int
    first_seen: int | None = None
    last_seen: int | None = None
    checks: list[tuple[str, int]] = field(default_factory=list)

    def check(self, cur: _Cursor, label: str, *, planned_bytes: int = 0) -> int:
        used = total_database_bytes(cur)
        if self.first_seen is None:
            self.first_seen = used
        self.last_seen = used
        self.checks.append((label, used))
        projected = used + planned_bytes
        if projected > self.ceiling_bytes:
            raise SystemExit(
                f"TỪ CHỐI CHẠY [{label}]: {projected / 1e6:.0f} MB vượt trần tự đặt "
                f"{self.ceiling_bytes / 1e6:.0f} MB. Service Aiven này nhỏ (gói 1 GB đĩa) và "
                "nó chở control plane ĐANG SỐNG của Loom — ép nó qua mép làm CẢ service "
                "chuyển sang chỉ-đọc, kể cả với lệnh DROP dọn dẹp (đã xảy ra thật). "
                "Xem docstring đầu _aiven_guard.py."
            )
        return used

    @property
    def delta(self) -> int:
        """Byte tăng thêm trong cả lần chạy. Phép đo chỉ-đọc phải cho ra 0."""
        if self.first_seen is None or self.last_seen is None:
            return 0
        return self.last_seen - self.first_seen
