"""DuckDB không biết cgroup, và nhu cầu bộ nhớ của nó co giãn theo SỐ LUỒNG.

Hai sự thật đó quyết định cấu hình của `loom-query` ở Giai đoạn 2b, và cả hai
đều đo được chứ không phải suy đoán.

**1. `memory_limit` mặc định theo RAM MÁY CHỦ, không theo cgroup.** Trong container
384 MiB nó tưởng mình có nhiều GB và đi thẳng tới OOMKill. Đã kiểm trong cgroup
thật (`scripts/measure_duckdb_spill.py`): đặt tay 256MB thì tiến trình sống, để
mặc định kiểu 8GB thì hạt nhân giết — `Killed`.

**2. Cùng một query, cùng một hạn mức, kết quả đổi theo `threads`:**

    threads=1  4M dòng  limit 256MB  -> chạy xong
    threads=2  4M dòng  limit 256MB  -> chạy xong
    threads=4  4M dòng  limit 256MB  -> OutOfMemoryException

Nhiều luồng nghĩa là nhiều buffer song song, nghĩa là nhiều bộ nhớ. Hệ quả trực
tiếp cho 2b: **`loom-query` phải ghim `threads`, không chỉ `memory_limit`.** Một
pod 384 MiB chạy DuckDB với số luồng mặc định theo số core của node là đặt cược
vào phần cứng.

Đây cũng là lý do bản đầu của file này FLAKY và đỏ trên CI: nó có hai phép chạy
CÙNG một query rồi khẳng định cả hai đều OOM. Ở máy dev (nhiều core) cả hai OOM;
trên runner của GitHub thì phép đầu OOM còn phép sau chạy xong. Một cửa chặn
flaky tệ hơn không có cửa chặn. Bản này ghim `threads` để mọi khẳng định tất định.

**Bẫy khi đo, ghi lại để không dính lại:** `repeat('x', 512)` cho mọi dòng một
chuỗi GIỐNG HỆT nhau; nén từ điển làm 977 MiB teo còn gần bằng không và bài đo
báo "chạy tốt" trong khi chưa hề chạm bộ nhớ. Dữ liệu phải biến thiên thật.

Điều spec THẬT SỰ cần không phải "tràn ra đĩa" mà là **"không giết pod"**. Một
`OutOfMemoryException` là kết quả tốt — lỗi bắt được, `loom-query` biến thành câu
trả lời tử tế. Thứ không chấp nhận được là RSS phình tới khi hạt nhân ra tay.
"""

from pathlib import Path

import duckdb
import pytest

# Cấu hình mà `loom-query` sẽ chạy thật: hạn mức thấp hơn limit container 384Mi
# (chỗ trống là cho bộ nhớ tiến trình Python, Arrow buffer, libc arena), và số
# luồng GHIM — xem docstring đầu file.
LIMIT_MB = 256
THREADS = 2

# Hạn mức nhỏ tới mức không mập mờ. Đo ở threads=1, 4M dòng: 16/32/64MB đều OOM,
# 128MB thì chạy xong. 32 nằm giữa vùng OOM chứ không sát ranh giới.
TINY_LIMIT_MB = 32

# ~512B/dòng và KHÁC NHAU từng dòng. Chuỗi lặp lại nén từ điển về gần 0 và làm
# phép đo vô nghĩa — xem docstring đầu file.
WIDE_VARIED = "SELECT i, repeat(md5(i::VARCHAR), 16) AS pad FROM range({rows}) t(i)"


def test_duckdb_does_not_read_the_cgroup_limit() -> None:
    """Ghi lại HÀNH VI MẶC ĐỊNH, vì nó là lý do cả file này tồn tại.

    Mặc định DuckDB đặt `memory_limit` theo RAM của MÁY CHỦ, không theo cgroup.
    Trong container 384 MiB nó tưởng mình có nhiều GB và đi thẳng tới OOMKill.
    Nếu một ngày DuckDB học được cgroup thì phép này đỏ — đó là tin tốt cần đọc,
    không phải một phép kiểm cần sửa cho xanh.
    """
    with duckdb.connect() as conn:
        default = conn.execute("SELECT current_setting('memory_limit')").fetchone()
    assert default is not None
    assert default[0] not in {"0 bytes", ""}


def test_setting_the_limit_actually_takes_effect() -> None:
    """`SET memory_limit` im lặng với giá trị sai định dạng, nên đọc lại.

    So bằng BYTE, không bằng chuỗi: DuckDB nhận "256MB" (thập phân) rồi báo lại
    "244.1 MiB" (nhị phân). Một phép so chuỗi `startswith("256")` sẽ đỏ oan — đã
    dính đúng lỗi đó khi viết bản đầu.
    """
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{LIMIT_MB}MB'")
        got = conn.execute("SELECT current_setting('memory_limit')").fetchone()
    assert got is not None
    value, unit = got[0].split()
    as_bytes = float(value) * {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[unit]
    assert as_bytes == pytest.approx(LIMIT_MB * 1000 * 1000, rel=0.01)


def test_the_production_configuration_completes(tmp_path: Path) -> None:
    """Vế KHẲNG ĐỊNH, và nó là cấu hình `loom-query` sẽ chạy thật.

    Không có phép này, một `memory_limit` đặt thành 1 byte cũng làm phép từ chối
    bên dưới xanh — và lúc đó pod query không chạy nổi query nào.
    """
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{LIMIT_MB}MB'")
        conn.execute(f"SET threads={THREADS}")
        conn.execute(f"SET temp_directory='{tmp_path}'")
        conn.execute("SET preserve_insertion_order=false")

        result = conn.execute(
            f"SELECT count(*) FROM ({WIDE_VARIED.format(rows=1_000_000)} ORDER BY pad, i)"  # noqa: S608
        ).fetchone()

    assert result is not None
    assert result[0] == 1_000_000


def test_far_over_the_limit_raises_and_the_connection_survives(tmp_path: Path) -> None:
    """Hai khẳng định trong MỘT query, có chủ đích.

    Bản đầu tách làm hai test chạy cùng một query, và chính sự trùng lặp đó tạo ra
    flake trên CI. Chạy một lần rồi khẳng định cả hai vế thì vừa nhanh hơn vừa
    không còn chỗ cho hai lần chạy ra hai kết quả.

    Vế một: vượt xa hạn mức thì DuckDB NÉM, và ném một lỗi BẮT ĐƯỢC.
    Vế hai: connection còn dùng được sau đó — nếu không, ở Giai đoạn 2b một query
    nặng sẽ kéo theo mọi query khác trong cùng pod.
    """
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{TINY_LIMIT_MB}MB'")
        conn.execute("SET threads=1")
        conn.execute(f"SET temp_directory='{tmp_path}'")
        conn.execute("SET preserve_insertion_order=false")

        with pytest.raises(duckdb.OutOfMemoryException):
            conn.execute(
                f"SELECT count(*) FROM ({WIDE_VARIED.format(rows=1_000_000)} ORDER BY pad, i)"  # noqa: S608
            ).fetchone()

        after = conn.execute("SELECT 1 + 1").fetchone()

    assert after is not None
    assert after[0] == 2
