"""ĐO 3 của Giai đoạn 3a (CỬA CHẶN cuối) — đường NẠP, tách theo BỐN GIAI ĐOẠN.

Đây là một PHÉP ĐO, không phải một tính năng. Nó không có người dùng, không có
đường vào từ API, và không có gì trong `services/` gọi tới nó. Cái nó phải cho
ra là những con số THẬT, kể cả khi chúng trượt ngưỡng — nhất là khi chúng trượt.

## Ngưỡng, CHỐT TRƯỚC khi đo và không đổi sau khi thấy số

Nền của Giai đoạn 2c (`docs/measurements/2026-08-10-phase-2c-write-path-50gb.md`)
đo đường GHI thuần (Arrow trong RAM -> Iceberg) ở **24,5 MB/s**. Đường NẠP làm
đúng việc đó CỘNG một lần đọc từ Postgres nguồn, nên nó chỉ có thể chậm hơn. Câu
hỏi là chậm hơn BAO NHIÊU, và ngưỡng đã chốt là:

    >= 60% của 24,5 MB/s  =  >= 14,7 MB/s      ĐẠT
    <  14,7 MB/s                               KHÔNG ĐẠT — chi phí đọc nguồn
                                               đang nuốt cả đường ghi, và đó là
                                               một bài toán KHÁC, có phép điều
                                               tra riêng của nó.

**KHÔNG hạ ngưỡng để cho qua.** Nếu trượt, nói ra là trượt và chỉ vào giai đoạn
nào đang ăn thời gian.

MB/s ở đây là **byte Arrow THÔ** (`RecordBatch.nbytes`) chia cho đồng hồ tường
của vòng lặp lô — cùng đại lượng và cùng ranh giới mà 2c dùng
(`scripts/measure_write_path.py`, `run_batches`: bấm giờ từ lô đầu, KHÔNG tính
thiết lập). Đo bằng một đại lượng khác thì hai con số không so được với nhau.

## Bốn giai đoạn, và vì sao phải tách

  1. **Đọc nguồn**     — `next()` trên iterator của `PostgresConnector.read`:
                         một vòng `FETCH FORWARD` trên cursor CÓ TÊN phía Aiven,
                         cộng phép dựng `pa.RecordBatch` từ các dòng lấy về.
  2. **Biến đổi**      — `loom_task.runner.add_bronze_columns`: ba cột bronze.
                         Đây là biến đổi THẬT của đường nạp, KHÁC hẳn 2c (chỗ đó
                         không có bước biến đổi nào và ghi thẳng 0.0s mỗi lô).
  3. **Ghi Iceberg**   — `tx.append(...)`: Parquet đi thẳng client -> S3.
  4. **Commit catalog**— `load_table` + `commit_transaction()`: một PUT REST tới
                         Lakekeeper, và Lakekeeper ghi vào Postgres của nó.

Cộng thêm một giai đoạn thứ NĂM mà 2c không có, và nó là nửa sau của Câu hỏi 1:

  5. **Báo tiến độ**   — `POST /internal/ingest/{run_id}/progress`. Với
                         `incremental` nó còn gồm `max(cột cursor)` của lô (đó
                         là chi phí CỦA việc báo watermark, không phải chi phí
                         của việc đọc), và phía server nó kéo theo cả
                         `_advance_watermark`. Với `full` nó chỉ mang `rows`.

Tách 3 và 4 đòi đi THẲNG vào PyIceberg thay vì qua `IcebergSink.append` —
`Lakehouse.append` gói cả hai bước làm một lời gọi. Xem `_append_timed`.

## Cái này KHÔNG chạy `loom_task.main.ingest`, và đây là giới hạn của nó

Vòng lặp dưới đây LẮP LẠI `run_incremental`/`run_full` từ chính những mảnh mà
đường thật dùng (`PostgresConnector`, `add_bronze_columns`, `staging_table_name`/
`old_target_name`, `Lakehouse.rename_table`/`drop_table`), nhưng nó KHÔNG gọi
`run_incremental`/`run_full`. Lý do là bấm giờ: hai hàm đó gọi `sink.append(...)`
— một lời gọi gộp ghi-file với commit-catalog — nên chạy chúng nguyên vẹn thì
giai đoạn 3 và 4 không tách ra được, mà tách chúng chính là cả điểm của ĐO 3.

Giá phải trả, nói thẳng: một thay đổi trong `runner.py` KHÔNG tự động chảy vào
đây. Cái làm rủi ro đó nhỏ là mọi mảnh ĐẮT đều được import chứ không chép lại —
chỉ có THỨ TỰ gọi là viết lại, và thứ tự đó được `services/loom-task/tests` canh
riêng.

## Nguồn: Aiven THẬT, và một hàng rào đĩa vì nó là gói MIỄN PHÍ

Nguồn duy nhất cụm với tới được là chính Aiven Postgres của chủ dự án (không có
Postgres nào trong cụm — xem `database` ở values.yaml). Đã ĐO trên chính service
đó trước khi viết script này, không suy đoán: `max_connections=20`,
`shared_buffers=190 MB`, `max_wal_size=49 MB` — hình dạng của gói 1 CPU/1 GB
RAM/**1 GB đĩa**. Tổng mọi database trên service lúc đo: 82 MB.

Nghĩa là đĩa nguồn là tài nguyên KHAN HIẾM, và nó dùng CHUNG với control plane
đang sống của Loom (`loom`) lẫn database của Lakekeeper. Lấp đầy nó không làm
hỏng phép đo — nó làm cả hệ thống của chủ dự án chuyển sang chỉ-đọc.

**VÀ ĐIỀU ĐÓ ĐÃ XẢY RA THẬT trong lúc dựng script này.** Lần nạp đầu tiên nhắm
1,2 triệu dòng; ở dòng thứ 1.000.000 (tổng mọi database 432 MB, riêng bảng bench
350 MB) Aiven chuyển cả service sang chỉ-đọc, và lệnh `DROP SCHEMA` dọn dẹp ngay
sau đó CŨNG bị từ chối. Trạng thái tự hết sau vài phút, nhưng trong khoảng đó
control plane của chủ dự án cũng không ghi được. Vì vậy hàng rào
(`check_source_disk`) chạy sau MỖI khối chứ không một lần duy nhất, trần đứng
DƯỚI mốc đã quan sát được (350 MB, không phải một con số tròn nghe hợp lý), và
bảng nguồn mặc định chỉ còn **500.000 dòng (~175 MB)**.

**HỆ QUẢ PHẢI GHI VÀO BÁO CÁO:** một bảng 175 MB nằm LỌT trong
`shared_buffers` + page cache của chính máy chủ Aiven, nên giai đoạn 1 đo được
là chi phí đọc một bảng ĐÃ NÓNG. Con số throughput vì thế là CẬN TRÊN — đường
nạp thật trên một bảng lớn hơn bộ nhớ chỉ có thể chậm hơn. Không có cách nào
tránh điều này trên gói này, nên nó được nói ra thay vì giấu đi.

## Vì sao chạy TRONG cụm, không trên host

Cùng lý do `scripts/measure_ingest_pod.py` chạy trong cụm, cộng một lý do nữa
riêng của ĐO 3: hostname Aiven chỉ phân giải được TỪ TRONG cụm (một ConfigMap
`coredns-custom` tạm thời, KHÔNG commit, chuyển tiếp tên miền đó về resolver nội
bộ). Chạy từ host thì không có nguồn để đọc. Và vì đã ở trong cụm, MinIO/
Lakekeeper gọi bằng DNS nội bộ (`http://minio:9000`,
`http://loom-lakekeeper:8181`) — không cần trò `kubectl port-forward` hai địa chỉ
của bản host.

## Nối lại được tới đâu — nói đúng, đừng hứa quá

`scripts/measure_write_path.py` ghi `progress.json` sau mỗi lô vì một lần chạy
50 GB mất hàng giờ trên host. Ở đây KHÔNG có cơ chế đó, và không phải vì quên:
tiến trình này sống trong một Job không có volume bền, nên "ghi tiến trình ra
đĩa" sẽ ghi vào một lớp container biến mất cùng pod. Cái nó có thay vào đó:

  - mỗi CẤU HÌNH là một lần gọi ĐỘC LẬP, tự dựng warehouse riêng và tự dọn sạch
    trong `finally`, nên chạy lại một cấu hình hỏng không cần dọn tay trước;
  - bảng NGUỒN tách hẳn khỏi vòng đo (`--seed-source` là một lần gọi riêng), nên
    ba cấu hình dùng CHUNG một bảng và chạy lại cấu hình thứ ba không phải sinh
    lại dữ liệu nguồn;
  - `--max-batches` để chạy thử nhanh trước khi chạy thật.

## Dọn dẹp — hai phía, và cả hai đều bắt buộc

Phía Iceberg: bỏ đăng ký bảng + namespace + xoá warehouse KHÔNG giải phóng đĩa —
2c đã đo điều đó trên Lakekeeper v0.9.2 (xem `Lakehouse.drop_table`). Chỉ
`purge_s3_prefix` (xoá thẳng qua S3 API) mới thật sự trả lại đĩa, và bỏ qua nó
nghĩa là mỗi lần chạy để lại vài trăm MB Parquet vĩnh viễn trên đĩa host.

Phía nguồn: `--drop-source` xoá schema bench trên Aiven rồi ĐỌC LẠI
`information_schema.schemata` để xác nhận nó đã biến mất — một lệnh DROP không
báo lỗi chưa phải một bằng chứng là đĩa đã được trả lại.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from resource import RUSAGE_SELF, getrusage
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import psycopg
import pyarrow as pa  # type: ignore[import-untyped]
from psycopg import sql
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

from loom_connector import StreamState
from loom_connector.postgres import PostgresConnector
from loom_core.internal_auth import INGEST_SHARED_SECRET_HEADER
from loom_core.schemas import IngestProgressReport
from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse
from loom_task.runner import add_bronze_columns, resolve_cursor
from loom_task.sink import old_target_name, staging_table_name

if TYPE_CHECKING:
    # `boto3-stubs` là dev dependency, và image `loom/task` build bằng
    # `uv sync --frozen --no-dev` nên KHÔNG có `mypy_boto3_s3` lúc chạy trong
    # pod — đã kiểm thật, cùng cạm bẫy đã ghi ở `scripts/measure_ingest_pod.py`.
    # Dưới `TYPE_CHECKING` thì mypy (chạy trên host, venv dev đầy đủ) vẫn kiểm
    # được kiểu còn runtime trong pod bỏ qua khối này.
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef


# ─────────────────────────── bảng nguồn ────────────────────────────

# Hình dạng dòng CHÉP TỪ `scripts/measure_write_path.py` có chủ đích: 2c đo
# throughput đường ghi trên đúng bảy cột này, và ĐO 3 so số của mình với số đó.
# Một bảng nguồn hình dạng khác (ít cột hơn, chuỗi ngắn hơn) sẽ cho một tỉ lệ
# nén Parquet khác và một byte/dòng khác, và phép so 60% mất nghĩa.
#
# Cả bảy cột ánh xạ sang kiểu Arrow ĐÚNG NGHĨA, KHÔNG cột nào đi qua `::text`
# (xem `_needs_text_cast` bên connector) — nghĩa là phép đo này canh ĐƯỜNG
# NHANH của connector. Một nguồn có `uuid`/`jsonb`/`numeric` sẽ tốn thêm phần
# kết xuất text phía Postgres; điều đó KHÔNG nằm trong con số dưới đây.
_SOURCE_DDL = sql.SQL(
    """
    CREATE TABLE {} (
        id           bigint PRIMARY KEY,
        event_time   timestamp with time zone NOT NULL,
        region       text NOT NULL,
        status       text NOT NULL,
        amount       double precision NOT NULL,
        customer_id  text NOT NULL,
        payload      text NOT NULL
    )
    """
)

REGIONS = [f"region-{i:02d}" for i in range(16)]  # lực lượng THẤP
STATUSES = ["pending", "processing", "completed", "failed", "refunded"]

# 220 ký tự hex cho cột đệm, cùng con số với `measure_write_path.py`. PHẢI ngẫu
# nhiên THẬT theo từng dòng: Giai đoạn 2a đã dính bẫy `repeat('x', N)` một lần —
# một cột giống hệt nhau nén từ điển gần về 0, và bài đo "chạy tốt" mà không
# chạm băng thông nào thật.
_PAYLOAD_HEX_CHARS = 220
_PAYLOAD_BITS = _PAYLOAD_HEX_CHARS * 4

# Mốc thời gian cơ sở + bước nhảy. KHÔNG dùng `infinity` hay một khoảng năm quá
# rộng: psycopg ném `DataError: timestamp too large` TRƯỚC khi pyarrow thấy giá
# trị — một dư chấn đã biết của việc ép kiểu, và không phải thứ ĐO 3 tồn tại để
# vấp phải. Một giây mỗi dòng, bắt đầu 2024-01-01, nên 1,2 triệu dòng trải ~14
# ngày và không đi đâu gần biên của kiểu.
_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)


def _source_rows(offset: int, count: int, seed: int) -> Iterator[tuple[Any, ...]]:
    """Sinh từng dòng một, KHÔNG dựng danh sách 1,2 triệu phần tử.

    `COPY` của psycopg nhận từng dòng, nên không có lý do gì để giữ cả lô trong
    RAM — và tiến trình sinh dữ liệu chạy trong cùng một pod với phép đo, nơi
    RAM là thứ đang được canh.
    """
    rng = random.Random(seed)  # noqa: S311 - dữ liệu đo, không phải mật mã
    for i in range(offset, offset + count):
        yield (
            i,
            _BASE_TIME + timedelta(seconds=i),
            REGIONS[i % len(REGIONS)],
            STATUSES[rng.randrange(len(STATUSES))],
            rng.uniform(0.01, 25_000.0),
            f"cust-{rng.getrandbits(64):016x}",
            f"{rng.getrandbits(_PAYLOAD_BITS):0{_PAYLOAD_HEX_CHARS}x}",
        )


def _connect(dsn: str) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(dsn, connect_timeout=15)


def _all_databases_bytes(cur: psycopg.Cursor[tuple[Any, ...]]) -> int:
    cur.execute("SELECT COALESCE(SUM(pg_database_size(datname)), 0) FROM pg_database")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def check_source_disk(
    cur: psycopg.Cursor[tuple[Any, ...]], *, planned_bytes: int, ceiling_mb: int, label: str
) -> None:
    """TỪ CHỐI CHẠY nếu service Aiven sắp vượt trần tự đặt.

    Hàng rào DUY NHẤT chặn phép đo lấp đầy đĩa của một service nhỏ đang chở
    CONTROL PLANE ĐANG SỐNG của Loom. Vai trò giống `check_disk_space` bên
    `scripts/measure_write_path.py`, nhưng bài học đắt hơn — xem dưới.

    **ĐÃ VỠ THẬT MỘT LẦN, và bản này là phép sửa.** Bản đầu kiểm ĐÚNG MỘT LẦN
    trước khi nạp, với ước lượng 1,2 triệu dòng x 330 byte = 396 MB trên 82 MB
    đang dùng, tổng dự kiến 478 MB — dưới trần 600 MB, nên nó cho chạy. Ở dòng
    thứ 1.000.000, Aiven CHUYỂN CẢ SERVICE SANG CHỈ-ĐỌC: `COPY` ném
    `ReadOnlySqlTransaction`, và ngay sau đó cả `DROP SCHEMA` cũng bị từ chối —
    tức là phép đo tự nhốt mình, không dọn được cái nó vừa tạo ra. Đo lại lúc
    đó: tổng mọi database 432 MB, riêng bảng bench 350 MB. Trạng thái chỉ-đọc
    tự hết sau vài phút (khi WAL được thu hồi), nhưng trong khoảng đó control
    plane của chủ dự án cũng không ghi được.

    Hai chỗ sai của bản đầu, và cả hai được sửa ở đây:

    1. **Kiểm một lần là không đủ.** Dung lượng đi lên TRONG LÚC nạp, và cái
       đẩy service qua mép không chỉ là dữ liệu — WAL của chính lần nạp cộng
       vào cùng một volume. Bản này gọi lại sau MỖI khối.
    2. **Trần 600 MB là suy đoán, không phải số đo.** Không có API nào trong
       SQL cho biết dung lượng gói, nên trần phải đứng dưới mốc ĐÃ QUAN SÁT
       được là nguy hiểm (432 MB), không đứng ở một con số tròn nghe hợp lý.

    So với TỔNG của MỌI database, không phải riêng database bench: đĩa là của
    service, và Lakekeeper ghi vào cùng volume đó ở mỗi commit catalog — tức là
    trong suốt phép đo.
    """
    used = _all_databases_bytes(cur)
    projected = used + planned_bytes
    ceiling = ceiling_mb * 1_000_000
    print(
        f"đĩa nguồn [{label}]: đang dùng {used / 1e6:.0f} MB"
        + (f", còn thêm ~{planned_bytes / 1e6:.0f} MB" if planned_bytes else "")
        + f" => ~{projected / 1e6:.0f} MB, trần tự đặt {ceiling_mb} MB",
        flush=True,
    )
    if projected > ceiling:
        raise SystemExit(
            f"TỪ CHỐI CHẠY: {projected / 1e6:.0f} MB vượt trần {ceiling_mb} MB. Service Aiven "
            "này nhỏ và nó chở control plane ĐANG SỐNG của Loom — ép nó qua mép làm CẢ service "
            "chuyển sang chỉ-đọc, kể cả với lệnh DROP dọn dẹp (đã xảy ra thật, xem docstring). "
            "Giảm --source-rows, hoặc nâng --source-ceiling-mb chỉ khi đã TỰ KIỂM dung lượng "
            "thật của gói ở console Aiven."
        )


def seed_source(
    dsn: str, *, schema: str, table: str, rows: int, chunk: int, seed: int, ceiling_mb: int
) -> None:
    """Dựng bảng nguồn trên Aiven. Idempotent: có rồi thì báo và không đụng.

    350 byte/dòng KHÔNG phải một ước lượng trên giấy: nó là số ĐO được từ lần
    nạp đầu tiên (1.000.000 dòng -> `pg_total_relation_size` 350 MB, đã gồm cả
    index PRIMARY KEY). Bản trước đoán 330 và đoán thiếu.
    """
    qualified = sql.Identifier(schema, table)
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        if cur.fetchone() is not None:
            cur.execute(
                sql.SQL("SELECT count(*), pg_total_relation_size({}) FROM {}").format(
                    sql.Literal(f"{schema}.{table}"), qualified
                )
            )
            # `scalar_one` không có ở psycopg thuần, nhưng một `SELECT count(*)`
            # LUÔN trả đúng một dòng — `None` ở đây là một giả định đã vỡ, không
            # phải một trạng thái hợp lệ để đi tiếp bằng một nhánh im lặng.
            existing = cur.fetchone()
            assert existing is not None
            print(
                f"bảng nguồn {schema}.{table} ĐÃ CÓ: {existing[0]:,} dòng, "
                f"{existing[1] / 1e6:.0f} MB — không đụng vào",
                flush=True,
            )
            return

    bytes_per_row = 350
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            check_source_disk(
                cur,
                planned_bytes=rows * bytes_per_row,
                ceiling_mb=ceiling_mb,
                label="trước khi nạp",
            )
        conn.commit()

    t0 = time.perf_counter()
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            cur.execute(_SOURCE_DDL.format(qualified))
        conn.commit()

        copy_stmt = sql.SQL(
            "COPY {} (id, event_time, region, status, amount, customer_id, payload) FROM STDIN"
        ).format(qualified)
        written = 0
        while written < rows:
            # Nạp theo KHỐI và commit từng khối: một `COPY` nửa triệu dòng là
            # một transaction duy nhất, và trên `max_wal_size=49 MB` nó ép
            # Postgres giữ toàn bộ WAL của transaction đó cho tới lúc commit.
            # Chia nhỏ cho WAL được tái sử dụng giữa chừng.
            take = min(chunk, rows - written)
            with conn.cursor() as cur, cur.copy(copy_stmt) as copy:
                for row in _source_rows(written, take, seed + written):
                    copy.write_row(row)
            conn.commit()
            written += take
            # Kiểm lại SAU MỖI KHỐI, không chỉ một lần lúc đầu — xem
            # `check_source_disk` cho lần nó đã vỡ vì thiếu đúng dòng này.
            with conn.cursor() as cur:
                check_source_disk(
                    cur,
                    planned_bytes=(rows - written) * bytes_per_row,
                    ceiling_mb=ceiling_mb,
                    label=f"{written:,}/{rows:,} dòng",
                )
            conn.commit()

        with conn.cursor() as cur:
            cur.execute(sql.SQL("ANALYZE {}").format(qualified))
            # `FROM {}` là BẮT BUỘC ở đây và đã từng thiếu: không có nó,
            # `count(*)` đếm trên một dòng ngầm định và báo "1 dòng" cho một
            # bảng nửa triệu dòng — một con số SAI in ra như thể đã đo.
            cur.execute(
                sql.SQL("SELECT count(*), pg_total_relation_size({}) FROM {}").format(
                    sql.Literal(f"{schema}.{table}"), qualified
                )
            )
            final = cur.fetchone()
            assert final is not None  # xem chú thích ở nhánh "ĐÃ CÓ" phía trên
        conn.commit()

    print(
        f"bảng nguồn {schema}.{table}: {final[0]:,} dòng, {final[1] / 1e6:.0f} MB "
        f"(nạp trong {time.perf_counter() - t0:.1f}s)",
        flush=True,
    )


def drop_source(dsn: str, *, schema: str) -> None:
    """Xoá schema bench rồi ĐỌC LẠI để xác nhận — không tin lệnh, tin phép đọc."""
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
            )
            still_there = cur.fetchone() is not None
            used = _all_databases_bytes(cur)
    if still_there:
        raise SystemExit(f"DROP SCHEMA chạy xong nhưng {schema} VẪN còn — dọn tay")
    print(
        f"đã xoá schema {schema}, xác nhận bằng information_schema.schemata. "
        f"Tổng mọi database còn {used / 1e6:.0f} MB.",
        flush=True,
    )


# ─────────────────────────── báo tiến độ ────────────────────────────


class ProgressReporter:
    """`POST /internal/ingest/{run_id}/progress` với ĐÚNG thân request của pod thật.

    Dựng `IngestProgressReport` chứ không một `dict` tự lắp, cùng lý do
    `loom_task.client.IngestClient` làm thế: hai phép kiểm ở biên của model (ba
    trường cursor đi cùng nhau, giá trị đọc được dưới kiểu nó khai) phải chạy ở
    BÊN GỬI. Nếu phép đo gửi một hình dạng mà pod thật không gửi được, con số nó
    đo là con số của một đường không tồn tại.

    KHÔNG dùng thẳng `IngestClient`: lớp đó gọi `GET .../spec` để lấy
    `source_id`, và lời gọi đó CHUYỂN TRẠNG THÁI của run (`pending` ->
    `running`, xem docstring `ingest_spec`). Một phép đo không được phép làm đổi
    trạng thái một hàng dữ liệu thật chỉ để lấy một con số.

    `run_id` phải là một hàng `ingest_run` CÓ THẬT — handler trả 404 cho id lạ,
    và một phép đo bấm giờ đường 404 sẽ báo một con số nhỏ hơn thật rất nhiều
    (không có `_advance_watermark` nào chạy).

    **`cursor_offset` cộng vào giá trị watermark GỬI ĐI, và chỉ vào đó.** Nó
    không đụng dữ liệu đọc, không đụng số dòng, không đụng bất cứ giai đoạn nào
    khác. Nó tồn tại vì một cạm bẫy đo lường có thật: watermark của một stream
    được LƯU LẠI, nên cấu hình thứ HAI chạy trên cùng bảng báo về những giá trị
    THẤP HƠN mốc mà cấu hình thứ nhất để lại, `moves_forward` trả `False`, và
    `_advance_watermark` bỏ hẳn câu `UPDATE`. Cấu hình thứ hai khi đó đo một
    nhánh RẺ HƠN nhánh mà cấu hình thứ nhất đã đo — hai con số trông so được với
    nhau nhưng không phải. Cho mỗi cấu hình một offset tăng dần thì mọi cấu hình
    cùng đi qua nhánh CÓ ghi.
    """

    def __init__(
        self, base_url: str, run_id: uuid.UUID, shared_secret: str, cursor_offset: int = 0
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={INGEST_SHARED_SECRET_HEADER: shared_secret},
            timeout=30.0,
        )
        self._run_id = run_id
        self._cursor_offset = cursor_offset

    def report(
        self,
        *,
        rows: int,
        cursor_column: str | None = None,
        cursor_type: str | None = None,
        cursor_value: int | None = None,
    ) -> None:
        report = IngestProgressReport(
            rows=rows,
            cursor_column=cursor_column,
            cursor_type=cursor_type,
            cursor_value=None if cursor_value is None else str(cursor_value + self._cursor_offset),
        )
        response = self._client.post(
            f"/internal/ingest/{self._run_id}/progress",
            json=report.model_dump(mode="json"),
        )
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()


def probe_progress_cost(
    reporter: ProgressReporter, *, pairs: int, cursor_base: int
) -> dict[str, float]:
    """Chi phí RIÊNG của việc báo watermark, đo bằng cặp so le.

    Đây là nửa "chi phí MỖI LÔ" của Câu hỏi 1, và nó cần một phép đo riêng chứ
    không đọc ra được từ vòng lặp chính: hai mode chạy ở hai thời điểm khác
    nhau, nên chênh lệch giữa hai lần chạy trộn lẫn chi phí watermark với nhiễu
    của mạng và của Aiven ở hai lúc khác nhau.

    So le `rows`-một-mình với `rows`+cursor trong CÙNG một vòng lặp làm cả hai
    hình dạng gặp cùng một điều kiện mạng. Lấy TRUNG VỊ chứ không trung bình:
    một cú GC hay một checkpoint của Aiven kéo trung bình đi rất xa, còn trung
    vị thì không.

    `cursor_value` TĂNG NGHIÊM NGẶT qua từng cặp, và đó là điều kiện để phép đo
    có nghĩa: `_advance_watermark` chỉ chạy `UPDATE` khi `moves_forward` đúng,
    nên một giá trị đứng yên sẽ đo nhánh RẺ (chỉ đọc, không ghi) và báo chi phí
    watermark thấp hơn thật.

    `rows=0`: `IngestProgressReport.rows` khai `ge=0` nên 0 hợp lệ, và nó giữ
    `ingest_run.rows_written` của run thật không bị phép đo này thổi lên.
    """
    rows_only: list[float] = []
    with_cursor: list[float] = []
    for i in range(pairs):
        t0 = time.perf_counter()
        reporter.report(rows=0)
        rows_only.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        reporter.report(
            rows=0,
            cursor_column="id",
            cursor_type="bigint",
            cursor_value=cursor_base + i,
        )
        with_cursor.append(time.perf_counter() - t0)

    median_rows = statistics.median(rows_only)
    median_cursor = statistics.median(with_cursor)
    return {
        "pairs": float(pairs),
        "rows_only_median_ms": median_rows * 1000,
        "with_cursor_median_ms": median_cursor * 1000,
        "watermark_extra_ms": (median_cursor - median_rows) * 1000,
    }


# ─────────────────────────── ghi Iceberg, tách giai đoạn ────────────────────────────


def _append_timed(catalog: RestCatalog, identifier: str, data: pa.Table) -> tuple[float, float]:
    """Ghi MỘT lô, trả `(giây_ghi_file, giây_commit_catalog)` TÁCH RIÊNG.

    Cùng phép tách mà `scripts/measure_write_path.py::append_batch_timed` dùng,
    và cùng lý do (đọc docstring ở đó cho phần đã xác minh trong mã nguồn
    PyIceberg 0.11.x): `tx.append` ghi data file Parquet thẳng lên S3 và chỉ XẾP
    một `AppendFiles` vào bộ nhớ; `commit_transaction()` mới gửi PUT tới
    Lakekeeper.

    `load_table` TÍNH VÀO commit: nó là một round trip REST tới Lakekeeper (chứ
    không phải I/O dữ liệu), và optimistic concurrency bắt buộc phải biết
    snapshot hiện tại trước khi dựng được một commit hợp lệ. `IcebergSink._write`
    cũng trả giá đó mỗi lô, qua `Lakehouse.append`.
    """
    t0 = time.perf_counter()
    table = catalog.load_table(identifier)
    tx = table.transaction()
    load_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    tx.append(data)
    write_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    tx.commit_transaction()
    commit_s = load_s + (time.perf_counter() - t0)

    return write_s, commit_s


def purge_s3_prefix(
    *, s3_endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str
) -> int:
    """Xoá THẲNG mọi object dưới `prefix` — bước DUY NHẤT thật sự giải phóng đĩa.

    2c đã đo trên Lakekeeper v0.9.2: `drop_table` + `drop_namespace` + xoá cả
    warehouse xong, `list_objects_v2` vẫn thấy nguyên data/metadata file dưới
    prefix của warehouse đó.
    """
    client = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )
    deleted = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys: list[ObjectIdentifierTypeDef] = [
            {"Key": obj["Key"]} for obj in page.get("Contents", [])
        ]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
    return deleted


def _s3_key_of(location: str, bucket: str) -> str:
    """`s3://<bucket>/<key>` -> `<key>`. Dùng để purge ĐÚNG bảng, không cả warehouse."""
    prefix = f"s3://{bucket}/"
    if not location.startswith(prefix):
        raise ValueError(f"location {location!r} không nằm trong bucket {bucket!r}")
    return location[len(prefix) :].rstrip("/")


# ─────────────────────────── kết quả ────────────────────────────


@dataclass
class BatchRecord:
    index: int
    rows: int
    raw_bytes: int
    read_s: float
    transform_s: float
    iceberg_write_s: float
    catalog_commit_s: float
    progress_s: float


@dataclass
class SwapRecord:
    """Chi phí CỐ ĐỊNH của `full` — một lần mỗi lần chạy, KHÔNG co giãn theo số lô.

    Đây là nửa "chi phí cố định" của Câu hỏi 1. Bốn lời gọi catalog cộng lại, và
    `purge_s3_s` tách riêng vì Giai đoạn 3a **KHÔNG** chạy nó: `IcebergSink.
    drop_old_target` chỉ bỏ TÊN khỏi catalog, còn object trên S3 ở lại (nợ có
    tên ở spec mục 13). Đo nó ở đây để biết món nợ đó đáng bao nhiêu, chứ không
    để nói rằng 3a đã trả.
    """

    staging_done_s: float
    target_exists_s: float
    rename_target_away_s: float
    promote_staging_s: float
    drop_old_target_s: float
    purge_s3_s: float
    purged_objects: int
    had_target: bool


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _summarise(
    *,
    mode: str,
    batch_rows: int,
    batches: list[BatchRecord],
    loop_s: float,
    swap: SwapRecord | None,
    progress_probe: dict[str, float] | None,
    compressed_bytes: int,
    threshold_mb_s: float,
) -> dict[str, Any]:
    raw_bytes = sum(b.raw_bytes for b in batches)
    rows = sum(b.rows for b in batches)
    read_s = sum(b.read_s for b in batches)
    transform_s = sum(b.transform_s for b in batches)
    write_s = sum(b.iceberg_write_s for b in batches)
    commit_s = sum(b.catalog_commit_s for b in batches)
    progress_s = sum(b.progress_s for b in batches)
    stage_sum = read_s + transform_s + write_s + commit_s + progress_s

    # Mẫu số là ĐỒNG HỒ TƯỜNG của vòng lặp, không phải tổng năm giai đoạn: hai
    # số này lệch nhau đúng bằng phần không thuộc giai đoạn nào (vòng lặp
    # Python, cấp phát), và giấu phần đó đi sẽ báo throughput cao hơn thật.
    throughput = (raw_bytes / 1e6) / loop_s if loop_s > 0 else 0.0
    peak_rss_mib = getrusage(RUSAGE_SELF).ru_maxrss / 1024

    def pct(x: float) -> float:
        return 100 * x / stage_sum if stage_sum > 0 else 0.0

    print("", flush=True)
    print(f"=== KẾT QUẢ: mode={mode} batch_rows={batch_rows:,} ===", flush=True)
    print(f"Lô: {len(batches)}   Dòng: {rows:,}", flush=True)
    print(f"Byte Arrow thô:        {raw_bytes / 1e9:.3f} GB", flush=True)
    if compressed_bytes > 0:
        print(
            f"Parquet nén trên S3:   {compressed_bytes / 1e9:.3f} GB "
            f"(nén {raw_bytes / compressed_bytes:.2f}x)",
            flush=True,
        )
    print(f"1. Đọc nguồn:      {read_s:8.1f}s  ({pct(read_s):5.1f}%)", flush=True)
    print(f"2. Biến đổi:       {transform_s:8.1f}s  ({pct(transform_s):5.1f}%)", flush=True)
    print(f"3. Ghi Iceberg:    {write_s:8.1f}s  ({pct(write_s):5.1f}%)", flush=True)
    print(f"4. Commit catalog: {commit_s:8.1f}s  ({pct(commit_s):5.1f}%)", flush=True)
    print(f"5. Báo tiến độ:    {progress_s:8.1f}s  ({pct(progress_s):5.1f}%)", flush=True)
    print(f"Vòng lặp (đồng hồ tường): {loop_s:.1f}s = {_fmt(loop_s)}", flush=True)
    if batches:
        med_commit = statistics.median(b.catalog_commit_s for b in batches)
        med_read = statistics.median(b.read_s for b in batches)
        med_progress = statistics.median(b.progress_s for b in batches)
        print(
            f"Mỗi lô, trung vị: commit={med_commit:.3f}s đọc={med_read:.3f}s "
            f"báo={med_progress:.3f}s",
            flush=True,
        )
    if swap is not None:
        catalog_swap_s = (
            swap.staging_done_s
            + swap.target_exists_s
            + swap.rename_target_away_s
            + swap.promote_staging_s
            + swap.drop_old_target_s
        )
        print(
            f"Cú tráo (CHI PHÍ CỐ ĐỊNH của full): {catalog_swap_s:.3f}s "
            f"[staging_done={swap.staging_done_s:.3f} target_exists={swap.target_exists_s:.3f} "
            f"rename_away={swap.rename_target_away_s:.3f} promote={swap.promote_staging_s:.3f} "
            f"drop_old={swap.drop_old_target_s:.3f}]  had_target={swap.had_target}",
            flush=True,
        )
        print(
            f"Purge S3 bảng cũ (3a KHÔNG chạy bước này): {swap.purge_s3_s:.3f}s cho "
            f"{swap.purged_objects} object",
            flush=True,
        )
    if progress_probe is not None:
        print(
            f"Chi phí báo watermark (cặp so le, n={int(progress_probe['pairs'])}): "
            f"rows-một-mình={progress_probe['rows_only_median_ms']:.1f}ms  "
            f"rows+cursor={progress_probe['with_cursor_median_ms']:.1f}ms  "
            f"CHÊNH={progress_probe['watermark_extra_ms']:.1f}ms/lô",
            flush=True,
        )
    verdict = "ĐẠT" if throughput >= threshold_mb_s else "KHÔNG ĐẠT"
    print(
        f"THROUGHPUT {throughput:.1f} MB/s   ngưỡng {threshold_mb_s:.1f} MB/s   {verdict}",
        flush=True,
    )
    print(f"RSS đỉnh: {peak_rss_mib:.0f} MiB", flush=True)

    return {
        "mode": mode,
        "batch_rows": batch_rows,
        "batches": len(batches),
        "rows": rows,
        "raw_bytes": raw_bytes,
        "compressed_bytes": compressed_bytes,
        "read_s": read_s,
        "transform_s": transform_s,
        "iceberg_write_s": write_s,
        "catalog_commit_s": commit_s,
        "progress_s": progress_s,
        "loop_s": loop_s,
        "throughput_mb_s": throughput,
        "threshold_mb_s": threshold_mb_s,
        "verdict": verdict,
        "peak_rss_mib": peak_rss_mib,
        "swap": asdict(swap) if swap is not None else None,
        "progress_probe": progress_probe,
        "per_batch": [asdict(b) for b in batches],
    }


# ─────────────────────────── một cấu hình ────────────────────────────


@dataclass
class Bench:
    """Mọi thứ một lần đo cần, dựng một lần rồi truyền đi."""

    catalog: RestCatalog
    lakehouse: Lakehouse
    bucket: str
    key_prefix: str
    warehouse_id: str
    warehouse_name: str
    s3_endpoint: str
    access_key: str
    secret_key: str
    namespace: str
    created: list[str] = field(default_factory=list)


def _fresh_warehouse(args: argparse.Namespace, access_key: str, secret_key: str) -> Bench:
    management_url = args.lakekeeper_url
    ensure_bootstrapped(management_url)

    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    key_prefix = prefix_for_lakehouse(workspace_id, lakehouse_id).rstrip("/")
    warehouse_name = f"bench-ingest-{uuid.uuid4().hex[:10]}"
    warehouse_id = create_warehouse(
        management_url,
        name=warehouse_name,
        bucket=args.bucket,
        key_prefix=key_prefix,
        s3_endpoint=args.minio_endpoint,
        access_key=access_key,
        secret_key=secret_key,
    )
    print(f"warehouse: {warehouse_name} ({warehouse_id}) prefix={key_prefix}", flush=True)

    catalog = build_catalog(
        catalog_uri=f"{management_url}/catalog",
        warehouse=warehouse_name,
        s3_endpoint=args.minio_endpoint,
    )
    lakehouse = Lakehouse(catalog)
    lakehouse.create_namespace_if_not_exists(args.iceberg_namespace)
    return Bench(
        catalog=catalog,
        lakehouse=lakehouse,
        bucket=args.bucket,
        key_prefix=key_prefix,
        warehouse_id=warehouse_id,
        warehouse_name=warehouse_name,
        s3_endpoint=args.minio_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        namespace=args.iceberg_namespace,
    )


def _teardown(bench: Bench, management_url: str) -> None:
    """Best-effort, và MỌI bước bọc riêng: dọn dẹp chạy SAU khi số đã chốt, nên
    một lỗi dọn không được phép đổi mã thoát của một phép đo đã tính đúng — đúng
    cái bẫy `probe_iceberg_single_commit.py` đã ăn một lần (Lakekeeper trả 403,
    không phải 404, cho một bảng đã bị rename đi mất).

    Liệt kê THẬT qua `list_tables` chứ không theo danh sách tên đã tạo: cú tráo
    của `full` đổi tên bảng giữa chừng, nên danh sách tên đã tạo không còn khớp
    thực tế.
    """
    try:
        for info in bench.lakehouse.list_tables(bench.namespace):
            try:
                bench.catalog.drop_table(info.qualified)
            except Exception as exc:
                print(f"  dọn: drop_table {info.qualified} lỗi {type(exc).__name__}", flush=True)
    except Exception as exc:
        print(f"  dọn: list_tables lỗi {type(exc).__name__}", flush=True)
    try:
        bench.catalog.drop_namespace(bench.namespace)
    except (NoSuchNamespaceError, NoSuchTableError):
        pass
    except Exception as exc:
        print(f"  dọn: drop_namespace lỗi {type(exc).__name__}", flush=True)
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.delete(f"{management_url}/management/v1/warehouse/{bench.warehouse_id}")
        if resp.status_code not in (204, 404):
            print(f"  dọn: xoá warehouse trả {resp.status_code}", flush=True)
    except Exception as exc:
        print(f"  dọn: xoá warehouse lỗi {type(exc).__name__}", flush=True)
    try:
        deleted = purge_s3_prefix(
            s3_endpoint=bench.s3_endpoint,
            access_key=bench.access_key,
            secret_key=bench.secret_key,
            bucket=bench.bucket,
            prefix=bench.key_prefix,
        )
        print(f"  dọn: xoá {deleted} object S3 dưới {bench.key_prefix}", flush=True)
    except Exception as exc:
        print(f"  dọn: purge S3 lỗi {type(exc).__name__}", flush=True)


def run_config(args: argparse.Namespace, dsn: str, access_key: str, secret_key: str) -> int:
    stream = f"{args.source_schema}.{args.source_table}"
    connector = PostgresConnector(dsn, batch_rows=args.batch_rows, schema=args.source_schema)

    check = connector.check()
    if not check.ok:
        raise SystemExit(f"nguồn không nối được: {check.message}")

    # `discover()` MỘT lần, đúng như `loom_task.main.ingest` làm — và cursor lấy
    # từ chính `resolve_cursor` của đường thật, không tự đoán tên cột.
    streams = connector.discover()
    cursor = resolve_cursor(streams, stream, None)
    print(f"cursor: {cursor.name} ({cursor.cursor_type})", flush=True)

    reporter: ProgressReporter | None = None
    if args.run_id is not None:
        secret = os.environ.get("LOOM_INGEST_SHARED_SECRET")
        if not secret:
            raise SystemExit("có --run-id nhưng thiếu LOOM_INGEST_SHARED_SECRET")
        reporter = ProgressReporter(args.api_base_url, args.run_id, secret, args.cursor_offset)

    bench = _fresh_warehouse(args, access_key, secret_key)
    target = f"{args.iceberg_namespace}.{args.target_table}"
    run_id_for_names = args.run_id or uuid.uuid4()
    staging = staging_table_name(target, run_id_for_names)
    old_target = old_target_name(target, run_id_for_names)
    # `full` ghi vào staging, `incremental` ghi thẳng vào đích — đúng hợp đồng
    # của hai vòng lặp thật (`loom_task.runner`).
    write_into = staging if args.mode == "full" else target

    # `full` đọc lại từ ĐẦU (StreamState rỗng). `incremental` ở đây cũng đọc lại
    # từ đầu, và đó là CHỦ ĐÍCH: hai mode phải chạy trên CÙNG dữ liệu để so được.
    # Truyền `cursor_column` mà không truyền `cursor_value` tái hiện đúng lần nạp
    # `incremental` ĐẦU TIÊN của một stream — connector thêm `ORDER BY <cursor>`
    # (xem `_read_rows`), một chi phí THẬT mà `full` không trả.
    state = StreamState() if args.mode == "full" else StreamState(cursor_column=cursor.name)

    batches: list[BatchRecord] = []
    swap: SwapRecord | None = None
    probe: dict[str, float] | None = None
    source_id = str(run_id_for_names)

    try:
        if args.mode == "full" and args.pre_create_target:
            # Lần nạp `full` ĐẦU TIÊN của một lakehouse không có bảng đích, nên
            # cú tráo của nó bỏ qua `rename_target_away` và `drop_old_target` —
            # rẻ hơn hẳn trạng thái ỔN ĐỊNH. Chi phí cố định cần biết là chi phí
            # của lần nạp thứ HAI trở đi, nên dựng sẵn một "thế hệ trước" ở đây.
            # Nằm NGOÀI đồng hồ đo: nó là thiết lập, không phải một giai đoạn.
            seed_batch = pa.table(
                {
                    "id": pa.array([0], type=pa.int64()),
                    "event_time": pa.array([_BASE_TIME], type=pa.timestamp("us", tz="UTC")),
                    "region": pa.array(["region-00"], type=pa.string()),
                    "status": pa.array(["pending"], type=pa.string()),
                    "amount": pa.array([1.0], type=pa.float64()),
                    "customer_id": pa.array(["cust-0"], type=pa.string()),
                    "payload": pa.array(["00"], type=pa.string()),
                    "_ingested_at": pa.array([_BASE_TIME], type=pa.timestamp("us", tz="UTC")),
                    "_source": pa.array([source_id], type=pa.string()),
                    "_batch_id": pa.array([str(uuid.uuid4())], type=pa.string()),
                }
            )
            bench.lakehouse.create_from(target, seed_batch)
            print("đã dựng 'thế hệ trước' của bảng đích để cú tráo chạy đủ bốn bước", flush=True)

        reader = connector.read(stream, state)
        created = False
        loop_start = time.perf_counter()
        while True:
            t0 = time.perf_counter()
            try:
                batch = next(reader)
            except StopIteration:
                break
            read_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            enriched = add_bronze_columns(batch, source_id, uuid.uuid4())
            transform_s = time.perf_counter() - t0

            data = pa.Table.from_batches([enriched])
            if not created:
                # Bảng chưa có -> `create_table` rồi coi lô đầu như một append
                # bình thường. `IcebergSink._write` gọi `create_from` (create +
                # append trong một lời gọi) cho lô đầu; tách ra ở đây để giai
                # đoạn 3 và 4 của LÔ ĐẦU đo được như mọi lô khác. Bản thân
                # `create_table` nằm ngoài bốn giai đoạn — nó là thiết lập, xảy
                # ra đúng một lần và không co giãn theo dữ liệu.
                bench.catalog.create_table(write_into, schema=data.schema)
                created = True
            write_s, commit_s = _append_timed(bench.catalog, write_into, data)

            t0 = time.perf_counter()
            if reporter is not None:
                if args.mode == "full":
                    reporter.report(rows=batch.num_rows)
                else:
                    # `max()` trên cột cursor là chi phí CỦA việc báo watermark
                    # (`run_incremental` tính nó ngay trước lời gọi), không phải
                    # chi phí của việc đọc — nên nó nằm trong giai đoạn 5.
                    value = max(batch.column(cursor.name).to_pylist())
                    reporter.report(
                        rows=batch.num_rows,
                        cursor_column=cursor.name,
                        cursor_type=cursor.cursor_type,
                        cursor_value=int(value),
                    )
            progress_s = time.perf_counter() - t0

            batches.append(
                BatchRecord(
                    index=len(batches),
                    rows=batch.num_rows,
                    raw_bytes=int(batch.nbytes),
                    read_s=read_s,
                    transform_s=transform_s,
                    iceberg_write_s=write_s,
                    catalog_commit_s=commit_s,
                    progress_s=progress_s,
                )
            )
            if len(batches) % args.log_every == 0:
                done = sum(b.raw_bytes for b in batches)
                elapsed = time.perf_counter() - loop_start
                print(
                    f"[lô {len(batches):>4}] dòng={batch.num_rows:,} "
                    f"cum={done / 1e6:7.1f}MB tb={(done / 1e6) / elapsed:6.2f}MB/s "
                    f"| đọc={read_s:5.2f}s bđ={transform_s:5.2f}s ghi={write_s:5.2f}s "
                    f"commit={commit_s:5.2f}s báo={progress_s:5.2f}s",
                    flush=True,
                )
            if args.max_batches is not None and len(batches) >= args.max_batches:
                print(f"--max-batches={args.max_batches}: dừng sớm", flush=True)
                break

        if args.mode == "full":
            t0 = time.perf_counter()
            staged = bench.lakehouse.exists(staging)
            staging_done_s = time.perf_counter() - t0
            if not staged:
                raise SystemExit("không có bảng staging — nguồn đọc ra rỗng")

            t0 = time.perf_counter()
            had_target = bench.lakehouse.exists(target)
            target_exists_s = time.perf_counter() - t0

            rename_away_s = 0.0
            drop_old_s = 0.0
            purge_s = 0.0
            purged = 0
            old_location: str | None = None
            if had_target:
                old_location = bench.catalog.load_table(target).location()
                t0 = time.perf_counter()
                bench.lakehouse.rename_table(target, old_target)
                rename_away_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            bench.lakehouse.rename_table(staging, target)
            promote_s = time.perf_counter() - t0

            if had_target:
                t0 = time.perf_counter()
                bench.lakehouse.drop_table(old_target)
                drop_old_s = time.perf_counter() - t0
                if old_location is not None:
                    t0 = time.perf_counter()
                    purged = purge_s3_prefix(
                        s3_endpoint=bench.s3_endpoint,
                        access_key=bench.access_key,
                        secret_key=bench.secret_key,
                        bucket=bench.bucket,
                        prefix=_s3_key_of(old_location, bench.bucket),
                    )
                    purge_s = time.perf_counter() - t0

            swap = SwapRecord(
                staging_done_s=staging_done_s,
                target_exists_s=target_exists_s,
                rename_target_away_s=rename_away_s,
                promote_staging_s=promote_s,
                drop_old_target_s=drop_old_s,
                purge_s3_s=purge_s,
                purged_objects=purged,
                had_target=had_target,
            )

        # Đồng hồ tường của vòng lặp DỪNG ở đây: cú tráo là chi phí cố định, và
        # gộp nó vào mẫu số throughput sẽ trộn hai câu hỏi khác nhau. Nó được
        # báo riêng, đầy đủ, ở `_summarise`.
        loop_s = time.perf_counter() - loop_start

        if reporter is not None and args.progress_probe > 0:
            # Chạy SAU vòng lặp: nó đẩy watermark của run lên một giá trị tổng
            # hợp, và làm thế TRƯỚC vòng lặp sẽ khiến mọi lô sau đó rơi vào
            # nhánh "không tiến" và đo nhầm nhánh rẻ.
            probe = probe_progress_cost(
                reporter, pairs=args.progress_probe, cursor_base=args.source_rows * 10
            )

        compressed = 0
        try:
            compressed = bench.lakehouse.scan_size_bytes(target)
        except Exception as exc:
            print(f"scan_size_bytes lỗi {type(exc).__name__} — bỏ qua", flush=True)

        result = _summarise(
            mode=args.mode,
            batch_rows=args.batch_rows,
            batches=batches,
            loop_s=loop_s,
            swap=swap,
            progress_probe=probe,
            compressed_bytes=compressed,
            threshold_mb_s=args.threshold_mb_s,
        )
        print("RESULT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    finally:
        if reporter is not None:
            reporter.close()
        if args.keep:
            print(f"--keep: GIỮ warehouse {bench.warehouse_name} — dọn tay", flush=True)
        else:
            _teardown(bench, args.lakekeeper_url)


# ─────────────────────────── CLI ────────────────────────────


def _dsn_from_env() -> str:
    """DSN nguồn ghép từ biến môi trường do `secretKeyRef` tiêm vào pod.

    KHÔNG nhận DSN qua dòng lệnh: một chuỗi kết nối trên `argv` lộ ra trong
    `ps`, trong log của `kubectl create job`, và trong shell history. Cùng lý do
    `make infra-local-source-secret` đổi tên khoá bằng `jq` trên JSON thay vì
    truyền giá trị qua `--from-literal`.
    """
    missing = [
        name
        for name in ("BENCH_PG_USER", "BENCH_PG_PASSWORD", "BENCH_PG_HOST", "BENCH_PG_DBNAME")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"thiếu biến môi trường nguồn: {', '.join(missing)}")
    from urllib.parse import quote

    user = quote(os.environ["BENCH_PG_USER"], safe="")
    password = quote(os.environ["BENCH_PG_PASSWORD"], safe="")
    host = os.environ["BENCH_PG_HOST"]
    port = os.environ.get("BENCH_PG_PORT", "5432")
    database = quote(os.environ["BENCH_PG_DBNAME"], safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--seed-source", action="store_true", help="Dựng bảng nguồn trên Aiven")
    parser.add_argument("--drop-source", action="store_true", help="Xoá schema bench trên Aiven")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--batch-rows", type=int, default=10_000, help="Mặc định = mặc định THẬT")
    parser.add_argument("--source-schema", default="bench_ingest")
    parser.add_argument("--source-table", default="ingest_bench")
    # 500.000 dòng (~175 MB) và trần 350 MB: cả hai là hệ quả TRỰC TIẾP của lần
    # nạp đã đẩy service sang chỉ-đọc ở ~432 MB — xem `check_source_disk`.
    parser.add_argument("--source-rows", type=int, default=500_000)
    parser.add_argument("--source-chunk", type=int, default=50_000)
    parser.add_argument("--source-ceiling-mb", type=int, default=350)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--bucket", default="loom-local")
    parser.add_argument("--iceberg-namespace", default="bench_ingest")
    parser.add_argument("--target-table", default="ingest_path")
    parser.add_argument("--lakekeeper-url", default="http://loom-lakekeeper:8181")
    parser.add_argument("--minio-endpoint", default="http://minio:9000")
    parser.add_argument("--api-base-url", default="http://loom-api:8000")
    parser.add_argument(
        "--run-id",
        type=uuid.UUID,
        default=None,
        help="Hàng ingest_run CÓ THẬT — bật giai đoạn 5. Thiếu nó, giai đoạn 5 là 0,0s và "
        "báo cáo phải nói rõ nó KHÔNG được đo",
    )
    parser.add_argument("--progress-probe", type=int, default=25)
    parser.add_argument(
        "--cursor-offset",
        type=int,
        default=0,
        help="Cộng vào giá trị watermark GỬI ĐI (không đụng dữ liệu đọc). Cấu hình thứ hai "
        "trở đi trên cùng một stream PHẢI đặt nó cao hơn mốc mà cấu hình trước để lại — "
        "xem ProgressReporter",
    )
    parser.add_argument("--pre-create-target", action="store_true", default=True)
    parser.add_argument("--no-pre-create-target", dest="pre_create_target", action="store_false")
    parser.add_argument("--max-batches", type=int, default=None, help="Chạy thử nhanh")
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--threshold-mb-s", type=float, default=14.7)
    parser.add_argument("--keep", action="store_true", help="KHÔNG dọn — chỉ để gỡ lỗi")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dsn = _dsn_from_env()

    if args.drop_source:
        drop_source(dsn, schema=args.source_schema)
        return 0
    if args.seed_source:
        seed_source(
            dsn,
            schema=args.source_schema,
            table=args.source_table,
            rows=args.source_rows,
            chunk=args.source_chunk,
            seed=args.seed,
            ceiling_mb=args.source_ceiling_mb,
        )
        return 0

    # Credential GỐC MinIO: Lakekeeper cần chúng để tự AssumeRole hộ lúc tạo
    # warehouse (xem `create_warehouse`). Client PyIceberg không bao giờ thấy
    # cặp này. `KeyError` với traceback rõ hơn một 403 mù mờ nếu quên mount.
    access_key = os.environ["MINIO_ACCESS_KEY"]
    secret_key = os.environ["MINIO_SECRET_KEY"]
    return run_config(args, dsn, access_key, secret_key)


if __name__ == "__main__":
    sys.exit(main())
