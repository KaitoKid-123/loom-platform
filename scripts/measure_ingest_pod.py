"""Đo 1 của Giai đoạn 3a (CỬA CHẶN) — RAM của một lần ghi Iceberg TỪ TRONG CỤM.

Phép đo 50 GB của 2c cho 1500/1843 Mi, nhưng tiến trình ghi chạy trên HOST. Con số
343 Mi "còn dư" vì thế CHƯA trừ đi một tiến trình nạp nằm trong cụm. Task này trả
lời đúng một câu: một pod ghi Iceberg tốn bao nhiêu RSS, và cả node có còn dưới
1843 Mi không.

Chạy TRONG pod, in RSS đỉnh ra stdout. `make measure-ingest-pod` dựng Job, chờ,
đọc log, rồi đo cả node.

KHÔNG dùng `sys.getsizeof` hay `tracemalloc`: PyArrow cấp bộ nhớ NGOÀI heap Python
(buffer Arrow là vùng nhớ C++), nên hai công cụ đó báo thiếu rất nhiều. Chỉ
`resource.getrusage(RUSAGE_SELF).ru_maxrss` — con số hạt nhân thấy — mới là thứ
cgroup dùng để quyết định có giết tiến trình hay không.
"""

import argparse
import resource
import sys
import uuid

import pyarrow as pa


def make_batch(rows: int, batch_index: int) -> pa.RecordBatch:
    """~300 byte/dòng, KHÁC NHAU từng dòng.

    Chuỗi lặp lại bị nén từ điển về gần 0 và biến bài đo thành bài đo của một bài
    toán khác — Giai đoạn 2 đã dính đúng bẫy đó một lần với `repeat('x', 512)`.
    """
    base = batch_index * rows
    return pa.RecordBatch.from_pydict(
        {
            "id": pa.array([base + i for i in range(rows)], type=pa.int64()),
            "pad": pa.array(
                [uuid.uuid5(uuid.NAMESPACE_OID, str(base + i)).hex * 8 for i in range(rows)],
                type=pa.string(),
            ),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows-per-batch", type=int, default=200_000)
    parser.add_argument("--batches", type=int, default=20)
    args = parser.parse_args()

    total_rows = 0
    for i in range(args.batches):
        batch = make_batch(args.rows_per_batch, i)
        total_rows += batch.num_rows
        # Giữ ĐÚNG một lô sống tại một thời điểm. Nếu RSS vẫn bò lên theo số lô thì
        # chỗ rò không nằm ở mã này — đó chính là thứ cần biết.
        del batch
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(
            f"[lô {i + 1:3d}/{args.batches}] dòng={total_rows:,} RSS đỉnh={peak:,.0f} MiB",
            flush=True,
        )

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"KẾT QUẢ rss_peak_mib={peak:.0f} rows={total_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
