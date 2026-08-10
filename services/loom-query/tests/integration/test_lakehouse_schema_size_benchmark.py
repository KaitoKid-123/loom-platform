"""Phép đo Task 2 (Giai đoạn 2c) — kích thước/độ trễ của `GET .../schema` trên
một lakehouse 200 bảng x 30 cột THẬT, đứng sau quyết định `?depth=` ở
`loom_query.lakehouse_schema` (đọc docstring module đó cho kết luận).

Chạy:  make measure-lakehouse-schema

KHÔNG nằm trong bộ test thường (`make test-int` loại `not benchmark`): đây là
một phép ĐO, không phải một phép kiểm — nó không khẳng định gì về tính đúng
đắn (đã có `test_lakehouse_schema_select.py` cho việc đó), và mất hơn chục
giây tạo dữ liệu.

Viết dưới dạng module pytest, dùng chung bộ container mà integration test
khác dùng (`lakekeeper`/`s3_endpoint`/`warehouse_name`) — cùng lý do đã ghi ở
`packages/icebergkit/tests/integration/test_scan_planning_benchmark.py`: một
bộ container thứ hai cho ra một con số nói về một hệ thống không ai chạy.

**200 bảng THẬT, không ngoại suy** — 200 bảng tạo trong khoảng 15s trên máy
phát triển, một chi phí một-lần chấp nhận được cho một phép đo, nên không cần
giảm quy mô rồi ngoại suy (spec Giai đoạn 2c lưu ý phải NGOẠI SUY CÓ GHI RÕ nếu
quy mô phải giảm — ở đây không cần).
"""

from __future__ import annotations

import json
import statistics
import time
import uuid

import pyarrow as pa
import pytest

from loom_iceberg import Lakehouse, build_catalog
from loom_query.config import Settings
from loom_query.lakehouse_schema import Depth, build_schema_tree
from loom_query.schemas import LakehouseSchemaOut

pytestmark = [pytest.mark.integration, pytest.mark.benchmark]

N_TABLES = 200
N_COLUMNS = 30
ROUNDS = 5

# Ngưỡng "dưới ~1 giây" mà spec Giai đoạn 2c đặt ra cho phương án "trả cả cây
# một lần" — đặt TRƯỚC khi đo, để số liệu không bị đọc theo hướng mình muốn
# (cùng kỷ luật đã ghi ở `test_scan_planning_benchmark.py::CACHE_THRESHOLD_MS`).
LATENCY_THRESHOLD_S = 1.0


def _one_row(n_columns: int) -> pa.Table:
    return pa.table({f"col_{i}": pa.array([i], type=pa.int64()) for i in range(n_columns)})


def test_measure_schema_tree_size_and_latency(
    lakekeeper: str, s3_endpoint: str, warehouse_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    setup_lakehouse = Lakehouse(setup_catalog)
    setup_lakehouse.create_namespace("bench")

    start = time.perf_counter()
    for i in range(N_TABLES):
        setup_lakehouse.create_from(f"bench.t_{i:04d}", _one_row(N_COLUMNS))
    create_elapsed = time.perf_counter() - start

    settings = Settings(catalog_uri=f"{lakekeeper}/catalog", s3_endpoint=s3_endpoint)
    # `warehouse_name` (fixture) LÀ `str(lakehouse_id)` — quy ước mà
    # `runner.py`/`build_schema_tree` giả định (warehouse Lakekeeper đặt tên
    # theo `item.id`, xem docstring `runner.py`).
    lakehouse_id = uuid.UUID(warehouse_name)

    def fresh_tree(depth: Depth) -> LakehouseSchemaOut:
        # Catalog MỚI mỗi vòng — dùng lại một catalog "ấm" sẽ đo cache của
        # PyIceberg trong tiến trình, không phải chi phí THẬT mà một request
        # HTTP riêng biệt nhận (cùng lý do `test_scan_planning_benchmark.py`
        # dựng lại `Lakehouse` mỗi vòng).
        return build_schema_tree(lakehouse_id, settings=settings, depth=depth)

    tables_ms: list[float] = []
    columns_ms: list[float] = []
    tree: LakehouseSchemaOut | None = None
    for _ in range(ROUNDS):
        start = time.perf_counter()
        fresh_tree("tables")
        tables_ms.append((time.perf_counter() - start) * 1000)

        start = time.perf_counter()
        tree = fresh_tree("columns")
        columns_ms.append((time.perf_counter() - start) * 1000)

    assert tree is not None
    assert len(tree.namespaces) == 1
    assert len(tree.namespaces[0].tables) == N_TABLES
    first_table_columns = tree.namespaces[0].tables[0].columns
    assert first_table_columns is not None
    assert len(first_table_columns) == N_COLUMNS

    full_body = json.dumps(
        {
            "namespaces": [
                {
                    "name": ns.name,
                    "tables": [
                        {"name": t.name, "columns": [c.model_dump() for c in (t.columns or [])]}
                        for t in ns.tables
                    ],
                }
                for ns in tree.namespaces
            ]
        }
    )
    names_only_body = json.dumps(
        {
            "namespaces": [
                {"name": ns.name, "tables": [{"name": t.name} for t in ns.tables]}
                for ns in tree.namespaces
            ]
        }
    )

    def report(label: str, samples_ms: list[float]) -> float:
        ordered = sorted(samples_ms)
        p95 = ordered[int(len(ordered) * 0.95)] if len(ordered) > 1 else ordered[0]
        print(f"{label:30s} p50={statistics.median(ordered):8.1f}ms  p95={p95:8.1f}ms")
        return p95

    with capsys.disabled():
        print(
            f"\n\n{N_TABLES} bảng thật x {N_COLUMNS} cột thật "
            f"(Lakekeeper thật, tạo trong {create_elapsed:.1f}s)\n"
        )
        report("depth=tables (namespace+bảng)", tables_ms)
        columns_p95 = report("depth=columns (đầy đủ cột)", columns_ms)
        print(f"\nKích thước JSON depth=tables : {len(names_only_body) / 1024:6.1f} KB")
        print(f"Kích thước JSON depth=columns: {len(full_body) / 1024:6.1f} KB")
        verdict = (
            "VƯỢT ngưỡng — giữ mặc định depth=tables, đừng trả cả cây một lần"
            if columns_p95 / 1000 > LATENCY_THRESHOLD_S
            else "trong ngưỡng — một endpoint trả cả cây vẫn ổn"
        )
        print(f"\nNgưỡng {LATENCY_THRESHOLD_S:.0f}s cho depth=columns -> {verdict}\n")
