"""Phép đo 1 mục 1 — thời gian LẬP KẾ HOẠCH QUÉT một bảng Iceberg.

Chạy:  make measure-scan

KHÔNG nằm trong bộ test thường: nó mất vài phút và nó là một phép ĐO, không phải
một phép kiểm. Đánh dấu `benchmark` để `make test-int` bỏ qua.

Viết dưới dạng module pytest chứ không phải script độc lập, để tái dùng đúng bộ
container mà test tích hợp dùng. Dựng một bộ thứ hai chỉ để đo là vừa tốn vừa
cho ra một môi trường khác với môi trường đã kiểm — và lúc đó con số đo được nói
về một hệ thống không ai chạy.

**Đo cái gì, và vì sao tách hai phần.** Lập kế hoạch và đọc dữ liệu co giãn theo
hai chiều khác nhau: lập kế hoạch là NHIỀU request nhỏ tới catalog và metadata
(nhạy với độ trễ), đọc dữ liệu là ÍT request lớn tới object store (nhạy với băng
thông). Một con số tổng không trả lời được câu nào trong hai câu, và câu hỏi của
Giai đoạn 2 là câu đầu: "SQL tương tác dưới một giây" có khả thi không.

**Đường cơ sở này đo trên testcontainers cùng máy**, nên nó là cận dưới. Giá trị
của nó là làm MỐC SO SÁNH cho lúc chuyển MinIO ra VPS (spec Giai đoạn 2 mục 2.0)
— chênh lệch lúc đó chính là cái giá của chặng mạng.
"""

import statistics
import time
import uuid

import pyarrow as pa
import pytest

from loom_iceberg import Lakehouse, build_catalog

pytestmark = [pytest.mark.integration, pytest.mark.benchmark]

ROUNDS = 20
ROWS_PER_SNAPSHOT = 50_000
SNAPSHOTS = 20

# Ngưỡng đặt TRƯỚC khi đo, để số liệu không bị đọc theo hướng mình muốn.
# Xem docs/measurements/2026-08-06-phase-2a-baseline.md.
CACHE_THRESHOLD_MS = 500.0


def _sample(rows: int, offset: int) -> pa.Table:
    return pa.table(
        {
            "id": pa.array(range(offset, offset + rows), type=pa.int64()),
            "region": pa.array([f"r{i % 8}" for i in range(rows)], type=pa.string()),
            "amount": pa.array([float(i) for i in range(rows)], type=pa.float64()),
        }
    )


def test_measure_scan_planning(
    lakekeeper: str, s3_endpoint: str, warehouse_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def fresh() -> Lakehouse:
        # Catalog MỚI mỗi vòng: dùng lại một catalog ấm sẽ đo cache của PyIceberg
        # trong tiến trình, chứ không phải chi phí thật mà một pod query nhận cho
        # MỖI query. Giai đoạn 2b dựng lại catalog theo vòng đời credential.
        return Lakehouse(
            build_catalog(
                catalog_uri=f"{lakekeeper}/catalog",
                warehouse=warehouse_name,
                s3_endpoint=s3_endpoint,
            )
        )

    table = f"bench.wide_{uuid.uuid4().hex[:6]}"
    setup = fresh()
    setup.create_namespace("bench")
    setup.create_from(table, _sample(ROWS_PER_SNAPSHOT, 0))
    for n in range(1, SNAPSHOTS):
        # Lập kế hoạch phải đọc manifest của MỌI snapshot, nên đo trên một bảng
        # một-snapshot là đo một trường hợp không tồn tại trong thực tế.
        setup.append(table, _sample(ROWS_PER_SNAPSHOT, n * ROWS_PER_SNAPSHOT))

    plan_ms: list[float] = []
    read_ms: list[float] = []
    rows = 0
    for _ in range(ROUNDS):
        house = fresh()

        start = time.perf_counter()
        reader = house.scan(table)
        plan_ms.append((time.perf_counter() - start) * 1000)

        start = time.perf_counter()
        rows = reader.read_all().num_rows
        read_ms.append((time.perf_counter() - start) * 1000)

    expected = ROWS_PER_SNAPSHOT * SNAPSHOTS
    assert rows == expected, f"đọc {rows} dòng, mong {expected} — số đo không đáng tin"

    def report(label: str, samples: list[float]) -> float:
        ordered = sorted(samples)
        p95 = ordered[int(len(ordered) * 0.95)]
        print(
            f"{label:16s} p50={statistics.median(ordered):8.1f}ms  "
            f"p95={p95:8.1f}ms  max={ordered[-1]:8.1f}ms"
        )
        return p95

    with capsys.disabled():
        print(f"\n\nBảng: {ROWS_PER_SNAPSHOT:,} dòng x {SNAPSHOTS} snapshot, {ROUNDS} vòng\n")
        plan_p95 = report("Lập kế hoạch", plan_ms)
        report("Đọc dữ liệu", read_ms)
        verdict = "LẤP CACHE" if plan_p95 > CACHE_THRESHOLD_MS else "KHÔNG lấp cache"
        print(f"\nNgưỡng {CACHE_THRESHOLD_MS:.0f}ms → quyết định MetadataCache: {verdict}\n")
