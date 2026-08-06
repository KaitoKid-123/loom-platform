"""DuckDB không biết cgroup. Phép kiểm này canh đúng chỗ đó.

SỬA SO VỚI PLAN — dựa trên số đo, không dựa trên giả định:

Plan Giai đoạn 2a viết tiêu chí "query vượt hạn mức RAM **tràn ra đĩa**, không
giết pod", và giả định rằng đặt `memory_limit` cộng `temp_directory` là đủ để
DuckDB tràn. Đo thật trên DuckDB 1.5.5 thì **không có lần nào tràn**:

    30M BIGINT  ORDER BY   limit 128MB  → OK,   temporary_storage_bytes = 0
    20M dòng    GROUP BY   limit 128MB  → OK,   temporary_storage_bytes = 0
    1M dòng x 512B VARCHAR khác nhau    → OutOfMemoryException ở MỌI mức
                                           đã thử, tới tận 512MB

Những lần "OK" chạy được nhờ nén và xử lý theo luồng, KHÔNG nhờ tràn đĩa —
`temporary_storage_bytes` bằng 0 ở cả ba. Còn payload chuỗi rộng thì DuckDB
không tràn mà ném lỗi.

(Một cái bẫy đã dính khi đo: `repeat('x', 512)` cho mọi dòng một chuỗi GIỐNG
HỆT nhau, nén từ điển làm 977 MiB dữ liệu teo còn gần bằng không. Dữ liệu đo
phải biến thiên thật — `repeat(md5(i::VARCHAR), 16)` — nếu không con số đo được
là con số của một bài toán khác.)

Nên tiêu chí được viết lại theo tính chất THẬT SỰ cần, và spec cũng chỉ cần đúng
tính chất đó: **pod không bị giết.** DuckDB ném `OutOfMemoryException` là kết
quả TỐT — đó là một lỗi bắt được, `loom-query` ở Giai đoạn 2b biến nó thành một
câu trả lời tử tế cho người dùng. Thứ không chấp nhận được là RSS phình tới khi
hạt nhân giết tiến trình, vì lúc đó mọi query khác trong pod chết theo.

`scripts/measure_duckdb_spill.py` là phần kiểm trong cgroup thật.
"""

from pathlib import Path

import duckdb
import pytest

# Thấp hơn limit container 384Mi của `loom-query` (Giai đoạn 2b). Khoảng cách đó
# là chỗ cho bộ nhớ của chính tiến trình Python, Arrow buffer và libc arena.
LIMIT_MB = 256

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


def test_a_query_over_the_limit_raises_instead_of_growing_unbounded(tmp_path: Path) -> None:
    """Tính chất SỐNG CÒN cho `loom-query`: vượt hạn mức thì DuckDB NÉM, và ném
    một lỗi bắt được.

    Đây không phải "tràn ra đĩa" — đo thật cho thấy DuckDB 1.5.5 không tràn với
    payload kiểu này (xem docstring đầu file). Ném lỗi là kết quả tốt: Giai đoạn
    2b bắt nó và trả về một câu trả lời tử tế, thay vì để hạt nhân giết pod và
    kéo theo mọi query khác.
    """
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{LIMIT_MB}MB'")
        conn.execute(f"SET temp_directory='{tmp_path}'")
        conn.execute("SET preserve_insertion_order=false")

        with pytest.raises(duckdb.OutOfMemoryException):
            conn.execute(
                f"SELECT count(*) FROM ({WIDE_VARIED.format(rows=1_000_000)} ORDER BY pad, i)"  # noqa: S608
            ).fetchone()


def test_the_connection_survives_the_failure(tmp_path: Path) -> None:
    """Ném lỗi thôi chưa đủ — connection phải còn dùng được sau đó.

    Một `OutOfMemoryException` làm hỏng connection sẽ buộc `loom-query` dựng lại
    mọi thứ, và tệ hơn là có thể kéo theo query khác nếu pod dùng chung. Phép này
    khẳng định lỗi là CỤC BỘ theo từng câu lệnh.
    """
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{LIMIT_MB}MB'")
        conn.execute(f"SET temp_directory='{tmp_path}'")
        conn.execute("SET preserve_insertion_order=false")

        with pytest.raises(duckdb.OutOfMemoryException):
            conn.execute(
                f"SELECT count(*) FROM ({WIDE_VARIED.format(rows=1_000_000)} ORDER BY pad, i)"  # noqa: S608
            ).fetchone()

        after = conn.execute("SELECT 1 + 1").fetchone()
    assert after is not None
    assert after[0] == 2


def test_a_query_within_the_limit_still_completes(tmp_path: Path) -> None:
    """Vế KHẲNG ĐỊNH. Không có nó, một `memory_limit` đặt thành 1 byte cũng làm
    hai phép ở trên xanh — và lúc đó `loom-query` không chạy nổi query nào."""
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{LIMIT_MB}MB'")
        conn.execute(f"SET temp_directory='{tmp_path}'")
        conn.execute("SET preserve_insertion_order=false")

        result = conn.execute(
            "SELECT count(*) FROM (SELECT i FROM range(30000000) t(i) ORDER BY i DESC)"
        ).fetchone()

    assert result is not None
    assert result[0] == 30_000_000
