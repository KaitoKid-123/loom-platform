"""Phép đo 1 mục 3 (CỬA CHẶN) — DuckDB trong một cgroup thật.

Unit test ở `packages/icebergkit/tests/test_duckdb_memory.py` khẳng định DuckDB
TÔN TRỌNG `memory_limit` và ném lỗi bắt được. Script này khẳng định điều khác và
quan trọng hơn: khi `memory_limit` đặt thấp hơn limit của container, tiến trình
KHÔNG bị hạt nhân giết. Chỉ cgroup thật mới trả lời được câu đó.

SỬA SO VỚI PLAN: bản đầu đặt tên "spill" và kiểm `temporary_storage_bytes > 0`.
Đo thật trên DuckDB 1.5.5 cho thấy nó KHÔNG tràn ra đĩa với các workload này —
nó nén, xử lý theo luồng, hoặc ném lỗi. Tính chất mà spec thật sự cần là "không
giết pod", nên script đo đúng cái đó. Tên file giữ nguyên để khớp plan.

Chạy:  make measure-spill
"""

import argparse
import os
import resource
import sys

import duckdb

# ~512B mỗi dòng và KHÁC NHAU từng dòng. Chuỗi lặp lại bị nén từ điển về gần 0 và
# biến bài đo thành bài đo của một bài toán khác — đã dính đúng bẫy đó một lần.
WIDE_VARIED = "SELECT i, repeat(md5(i::VARCHAR), 16) AS pad FROM range({rows}) t(i)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--memory-limit",
        default="256MB",
        help="Hạn mức của DuckDB. PHẢI thấp hơn limit container — đó là cả điểm "
        "của bài đo. Giai đoạn 2b đặt container 384Mi, nên 256MB là con số thật.",
    )
    parser.add_argument("--temp-dir", default="/tmp/duckdb-spill")  # noqa: S108
    parser.add_argument("--rows", type=int, default=4_000_000)
    args = parser.parse_args()

    os.makedirs(args.temp_dir, exist_ok=True)
    print(f"memory_limit={args.memory_limit}  temp_directory={args.temp_dir}  rows={args.rows:,}")

    raised: str | None = None
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{args.memory_limit}'")
        conn.execute(f"SET temp_directory='{args.temp_dir}'")
        conn.execute("SET preserve_insertion_order=false")
        try:
            conn.execute(
                f"SELECT count(*) FROM ({WIDE_VARIED.format(rows=args.rows)} ORDER BY pad, i)"  # noqa: S608
            ).fetchone()
            print("query chạy xong trong hạn mức")
        except duckdb.OutOfMemoryException as exc:
            raised = str(exc).splitlines()[0]
            print(f"DuckDB từ chối trong hạn mức: {raised[:90]}")

        # Connection phải còn dùng được — nếu không, một query nặng sẽ kéo theo
        # mọi query khác trong cùng pod ở Giai đoạn 2b.
        alive = conn.execute("SELECT 1 + 1").fetchone()

    peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"RSS đỉnh: {peak_mib:,.0f} MiB")

    if alive is None or alive[0] != 2:
        print("KHÔNG ĐẠT — connection chết sau lỗi", file=sys.stderr)
        return 1

    # Tới được đây nghĩa là tiến trình còn sống: hạt nhân chưa giết nó. Đó CHÍNH
    # là tính chất cần — bị OOMKill thì Python không in nổi dòng nào.
    print("ĐẠT — tiến trình còn sống, connection còn dùng được, không bị OOMKill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
