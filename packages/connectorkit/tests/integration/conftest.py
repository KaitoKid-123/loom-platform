"""Dữ liệu mẫu cho bộ test riêng của `PostgresConnector`
(`test_postgres_connector.py`). `source_dsn` (container Postgres dùng chung
cho cả package) đến từ `packages/connectorkit/tests/conftest.py` — pytest tự
nối fixture theo cây thư mục, không cần import gì ở đây để dùng được nó.

Fixture ở đây trả TUPLE kiểu built-in (`str`/`int`), KHÔNG trả một dataclass
định nghĩa trong chính file này: `test_postgres_connector.py` là một file
ANH EM (cùng thư mục), không phải con của conftest.py, và thư mục này CỐ Ý
không có `__init__.py` (xem lý do ở dưới) — `from .conftest import Something`
sẽ đòi đúng cái `__init__.py` đó. `packages/storagekit/tests/integration/conftest.py`
đã dính lỗi y hệt và chọn cùng cách giải quyết: mọi thứ một test cần phải đi
qua THAM SỐ CỦA FIXTURE, không qua import tương đối.

## Vì sao KHÔNG có `__init__.py` ở thư mục `integration/` này

Giai đoạn 2a: `packages/*/tests/integration/__init__.py` từng trùng tên module
với `services/api/tests/integration/__init__.py`. Cả hai KHÔNG có `__init__.py`
ở `tests/` (thư mục cha), nên pytest (chế độ import "prepend" mặc định) coi
thư mục gốc để chèn vào `sys.path` là chính `tests/` của MỖI BÊN, và tên
module suy ra là `integration.<tên file>` cho CẢ HAI — hai `integration/__init__.py`
khác nhau cùng tranh một tên module `integration` trong `sys.modules`, và
`make test-int` chết ở bước collect với `ImportPathMismatchError`. Đã kiểm
lại bằng `make test-int` sau khi tạo các file này mà KHÔNG thêm `__init__.py`
— xem báo cáo Task 5 để biết kết quả collect thật.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql

_SCHEMA = "loom_ingest_test"
_STREAM_NAME = "loom_ingest_test.orders"
_N_ROWS = 25
_BATCH_ROWS = 10  # 25 / 10 => 3 lô (10, 10, 5) — đủ để bài đếm-lô có ý nghĩa

_LARGE_SCHEMA = "loom_ingest_large_test"
_LARGE_STREAM_NAME = "loom_ingest_large_test.big_rows"
_LARGE_N_ROWS = 50_000
_LARGE_BATCH_ROWS = 100
_LARGE_PAYLOAD_BYTES = 1000  # tổng ~50 MB payload text — xem docstring postgres.py
# về con số RSS đã đo bằng chính hình dạng dữ liệu này (0.4 MiB cursor có tên,
# 98.8 MiB cursor thường, cho ĐÚNG một lô).


@pytest.fixture(scope="module")
def seeded_source(source_dsn: str) -> Iterator[tuple[str, str, int, int]]:
    """Dựng MỘT bảng cố định trong schema riêng (`loom_ingest_test`), seed
    `_N_ROWS` dòng, dọn khi hết module. Trả `(dsn, stream_name, n_rows,
    batch_rows)`.

    Cột cố ý phủ đủ bốn thứ Task 5 đòi kiểm:
      - `id` (integer, NOT NULL, tăng dần)      — candidate cursor
      - `placed_at` (timestamptz, NOT NULL, tăng dần) — candidate cursor
      - `amount` (numeric, NOT NULL)             — PHẢI bị loại khỏi
        candidate_cursors, PHẢI về Arrow string (không phải float64)
      - `note` (text, NULLABLE)                  — kiểm nullable đọc từ
        nguồn thật chứ không đoán (id/placed_at/amount đều NOT NULL để làm
        đối chứng)
    """
    with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_SCHEMA)))
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(_SCHEMA)))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.orders ("
                "id integer NOT NULL, "
                "placed_at timestamptz NOT NULL, "
                "amount numeric(12,2) NOT NULL, "
                "note text)"
            ).format(sql.Identifier(_SCHEMA))
        )
        base = datetime(2024, 1, 1, tzinfo=UTC)
        cur.executemany(
            sql.SQL(
                "INSERT INTO {}.orders (id, placed_at, amount, note) VALUES (%s, %s, %s, %s)"
            ).format(sql.Identifier(_SCHEMA)),
            [
                (
                    i,
                    base + timedelta(minutes=i),
                    Decimal("19.99") + i,
                    None if i % 6 == 0 else f"note-{i}",
                )
                for i in range(_N_ROWS)
            ],
        )
    yield source_dsn, _STREAM_NAME, _N_ROWS, _BATCH_ROWS
    with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_SCHEMA)))


@pytest.fixture(scope="module")
def large_seeded_source(source_dsn: str) -> Iterator[tuple[str, str, int]]:
    """Bảng RIÊNG, lớn hơn nhiều (`_LARGE_N_ROWS` dòng, mỗi dòng ~1 KB
    payload — tổng ~50 MB), CHỈ để phục vụ
    `test_read_does_not_materialize_before_the_first_batch`. Tách khỏi
    `seeded_source` vì bài đó cần dữ liệu đủ NẶNG để chênh lệch RAM giữa
    "kéo một lô" và "kéo cả bảng" đo được rõ ràng — 25 dòng nhỏ như
    `seeded_source` không tạo ra chênh lệch nào đáng tin. Trả
    `(dsn, stream_name, batch_rows)`.
    """
    with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_LARGE_SCHEMA))
        )
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(_LARGE_SCHEMA)))
        cur.execute(
            sql.SQL("CREATE TABLE {}.big_rows (id integer NOT NULL, payload text NOT NULL)").format(
                sql.Identifier(_LARGE_SCHEMA)
            )
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.big_rows (id, payload) "
                "SELECT i, repeat('x', %s) FROM generate_series(1, %s) AS s(i)"
            ).format(sql.Identifier(_LARGE_SCHEMA)),
            (_LARGE_PAYLOAD_BYTES, _LARGE_N_ROWS),
        )
    yield source_dsn, _LARGE_STREAM_NAME, _LARGE_BATCH_ROWS
    with psycopg.connect(source_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_LARGE_SCHEMA))
        )
