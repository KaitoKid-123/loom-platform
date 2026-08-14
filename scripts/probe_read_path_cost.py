"""Đo 4 của Giai đoạn 3a — TÁCH chi phí ĐƯỜNG TRUYỀN khỏi chi phí BIẾN ĐỔI trên đường đọc.

Đây là một PHÉP ĐO, không phải một tính năng. Nó không có người dùng, không có
đường vào từ API, và không có gì trong `services/` gọi tới nó.

## Câu hỏi, và vì sao nó quyết định hướng đi

ĐO 3 (`docs/measurements/2026-08-13-phase-3a-ingest-path.md`) đo giai đoạn ĐỌC
NGUỒN của đường nạp ở trần **~7,3 MB/s** (149 MB byte Arrow / ~20,4 s ở
`batch_rows=100.000`), và KHÔNG tách được hai nguyên nhân có thể — hai nguyên
nhân chỉ về hai hướng NGƯỢC NHAU:

  * **Chặn bởi MẠNG/TLS.** Đường internet tới Aiven. Không sửa được bằng mã, và
    nếu đúng thì ngưỡng 14,7 MB/s (suy ra từ đường GHI 2c chạy HOÀN TOÀN cục bộ,
    không có nguồn từ xa nào) chưa bao giờ là một phép so sánh công bằng — ngưỡng
    phải viết lại.
  * **Chặn bởi BIẾN ĐỔI.** `PostgresConnector._read_rows` gom `list[dict]` từ
    psycopg rồi mới dựng `RecordBatch`. Sửa được: dựng theo CỘT thay vì theo DÒNG.

Script này đo bốn ô, cùng hình dạng dòng và cùng số dòng:

    | nguồn  | COPY ... TO STDOUT (thuần dây)  | qua đường của connector      |
    |--------|---------------------------------|------------------------------|
    | Aiven  | 1. trần MẠNG                    | 2. phải dựng lại ~7,3 MB/s   |
    | local  | 3. cùng đường dây, KHÔNG internet | 4. chi phí MÃ, bỏ mạng đi   |

và hai ô phụ tách tiếp ô 2/ô 4 làm ba lớp, vì "dây + psycopg + Arrow" là BA thứ
chứ không phải hai:

    copy_text/copy_binary  dây thuần, gần như không dựng object Python nào
    cursor_drain           dây + psycopg dựng một `dict` mỗi dòng, KHÔNG có Arrow
    connector              dây + dict + `_rows_to_record_batch`  <- đường ĐO 3 đo

Hiệu `connector - cursor_drain` là chi phí Arrow THUẦN; hiệu
`cursor_drain - copy_text` là chi phí dựng object psycopg thuần.

## TUYỆT ĐỐI KHÔNG GHI VÀO AIVEN — và điều đó được THI HÀNH, không phải hứa

Service Aiven nguồn là gói nhỏ (1 CPU / 1 GB RAM / 1 GB đĩa) và nó chở CONTROL
PLANE ĐANG SỐNG của Loom cùng database của Lakekeeper. ĐO 3 đã đẩy nó qua mép
một lần: seed 1,2 triệu dòng làm cả service chuyển sang CHỈ-ĐỌC trong lúc control
plane đang chạy, và chính lệnh dọn dẹp cũng bị từ chối.

Nên ở đây KHÔNG có `CREATE TABLE`, `INSERT`, `COPY FROM`, `DROP`, bảng tạm, hay
bất cứ thứ gì tiêu đĩa/sinh WAL. Dòng được sinh SERVER-SIDE bằng
`generate_series` — nó không chạm một page nào trên đĩa và không sinh một byte
WAL nào.

Và điều đó không nằm ở thiện chí của người viết: mọi connection tới Aiven mở với
`-c default_transaction_read_only=on` (xem `_aiven_dsn`), nên nếu một dòng nào đó
trong file này lỡ tay ghi, Postgres TỪ CHỐI nó thay vì thực hiện. Script cũng đọc
`sum(pg_database_size(...))` TRƯỚC và SAU cả lần chạy và in cả hai — bằng chứng
số, không phải lời khẳng định.

## "BYTE" ở đây là gì — hai đại lượng, KHÔNG được trộn

  * **byte Arrow** — `RecordBatch.nbytes`, biểu diễn trong RAM. Đây là đại lượng
    của ĐO 3 và của 2c, nên nó là MẪU SỐ CHÍNH ở mọi ô, kể cả các ô COPY (ở đó
    nó là "byte Arrow mà ngần ấy dòng SẼ dựng ra" = số dòng nhân byte/dòng đã hiệu
    chỉnh). Chỉ như thế bốn ô mới so được với nhau VÀ với 7,3 MB/s của ĐO 3.
  * **byte dây** — số byte thật sự nhận từ socket ở các ô COPY. In RIÊNG, không
    bao giờ thay chỗ byte Arrow. Hai con số khác nhau ~5-10% ở hình dạng này và
    trộn chúng là cách dễ nhất để rút ra một kết luận sai.

`cursor_drain`/`connector` KHÔNG đếm được byte dây (psycopg không phơi bộ đếm
socket nào), nên cột đó để trống ở hai ô đó — `copy_text` là ước lượng tốt nhất
cho khối lượng dây mà chúng chở, vì psycopg 3 nhận kết quả ở ĐỊNH DẠNG TEXT cho
cursor có tên (đã kiểm lúc chạy, in ra ở phần đầu báo cáo).

## Hình dạng dòng — giống ĐO 3 để số so được

Bảy cột y hệt bảng bench của ĐO 3 (và của 2c): `id bigint`, `event_time
timestamptz`, `region text` (16 giá trị), `status text` (5 giá trị), `amount
float8`, `customer_id text`, `payload text` 220 ký tự. ĐO 3 đo 500.000 dòng =
0,149 GB byte Arrow => ~298 byte/dòng; script này hiệu chỉnh lại con số đó từ dữ
liệu THẬT của chính nó và in ra để đối chiếu.

Cả bảy cột đi đường NHANH của connector (không cột nào bị `::text` — xem
`_needs_text_cast`), đúng như bảng của ĐO 3.

## Ba nhiễu ĐÃ BIẾT, và cách script này chặn/định lượng từng cái

  1. **`generate_series` tốn CPU server mà đọc một bảng đã lưu thì không.**
     Định lượng bằng `EXPLAIN (ANALYZE, TIMING OFF)` trên chính câu SELECT đó:
     nó chạy plan tới cùng, đánh giá ĐỦ mọi biểu thức trong target list, nhưng
     KHÔNG gửi dòng nào qua dây. Đó là CẬN DƯỚI của phần CPU server (nó bỏ qua
     chi phí hàm output kết xuất giá trị ra dạng dây), và cận dưới là đủ: nếu nó
     đã nhỏ so với đồng hồ tường thì nhiễu này không đổi được kết luận.
  2. **`md5()` mỗi dòng không miễn phí.** `explain_nomd5` chạy CÙNG câu đó với
     `customer_id`/`payload` thay bằng HẰNG chuỗi. Hiệu hai lần EXPLAIN là chi
     phí md5+rpad thuần.
  3. **Container local và Aiven khác phiên bản/phần cứng.** Container ghim
     `postgres:17-alpine` (Aiven chạy 17.10 — đã đọc `version()` lúc chạy và in
     ra). Phần cứng thì KHÔNG khử được và không được giả vờ là khử được: ô 3/ô 4
     chạy trên CPU của chính máy này, chia sẻ CPU với tiến trình client. Xem mục
     "nhiễu" của báo cáo.

Nhiễu thứ tư, đo được nên đo: ô 4 chạy `generate_series`, còn đường THẬT đọc một
BẢNG. `connector_real_table` (chỉ local — local ghi được) seed một bảng THẬT
đúng bảy cột đó rồi chạy `PostgresConnector.read()` NGUYÊN BẢN trên nó. Nó vừa
định lượng chênh lệch generate_series/bảng-thật, vừa là phép KIỂM CHÉO cho việc
lắp lại vòng lặp ở `_connector_path` bên dưới.

## Vì sao lắp lại vòng lặp thay vì gọi `PostgresConnector.read()` ở Aiven

`read()` đọc `information_schema.columns` để biết cột, rồi dựng
`SELECT ... FROM schema.table` — nó chỉ đọc được một BẢNG CÓ THẬT, và trên Aiven
không được phép tạo bảng nào. Nên `_connector_path` lắp lại ĐÚNG vòng lặp của
`_read_rows` (cursor CÓ TÊN, `dict_row`, `itersize == batch_rows`, gom
`list[dict]`, gọi `_rows_to_record_batch`) và chỉ đổi mệnh đề FROM.

Phần ĐẮT không bị chép lại — `_rows_to_record_batch` và `_ColumnInfo` được
IMPORT thẳng từ `loom_connector.postgres`, nên bước biến đổi đang đo LÀ mã
production chứ không phải một bản mô phỏng của nó. Phần bị viết lại chỉ là năm
dòng gom lô, và `connector_real_table` ở trên tồn tại để chứng minh năm dòng đó
không lệch.

## Chạy

    make probe-read-cost                        # cả bốn ô, 3 lần lặp
    make probe-read-cost ARGS="--rows 100000"   # chạy thử nhanh
    make probe-read-cost ARGS="--report-only"   # in lại tổng kết từ progress.json
    make probe-read-cost ARGS="--fresh"         # bỏ progress.json cũ, chạy lại từ đầu

Chạy từ HOST, không phải trong cụm (khác ĐO 3 — xem mục nhiễu của báo cáo): tên
miền Aiven phân giải được từ host (`make migrate` vẫn làm thế), và bỏ lớp mạng
của k3d ra khỏi phép đo là điều ta MUỐN ở đây, vì câu hỏi là "đường internet
đáng bao nhiêu", không phải "CNI của k3d đáng bao nhiêu".

## Nối lại được

`--state-dir/progress.json` ghi ATOMIC sau MỖI mẫu (`.tmp` rồi `rename`), đúng
cơ chế của `scripts/measure_write_path.py`. Đứt giữa chừng thì chạy lại đúng lệnh
cũ sẽ bỏ qua các ô đã đo xong. Thứ tự chạy là LẶP-TRƯỚC (rep 0 của mọi ô, rồi rep
1...) chứ không phải ô-trước: nhiễu mạng tới theo từng đợt, và đo ba lần liên
tiếp cùng một ô sẽ báo cáo một độ tản mát nhỏ hơn sự thật.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import resource
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from loom_connector.postgres import (
    PostgresConnector,
    _arrow_type_for,
    _ColumnInfo,
    _needs_text_cast,
    _rows_to_record_batch,
)
from loom_connector.protocol import StreamState

REPO_ROOT = Path(__file__).resolve().parents[1]

# ─────────────────────────── hình dạng dòng ────────────────────────────

# Tên cột + chuỗi `information_schema.columns.data_type` NGUYÊN VĂN — đúng thứ
# `PostgresConnector._columns_for` đọc được từ bảng bench của ĐO 3. Giữ chuỗi
# nguồn (chứ không giữ sẵn kiểu Arrow) để `_ColumnInfo` dựng dưới đây đi qua
# CHÍNH `_arrow_type_for`/`_needs_text_cast` của production: nếu một ngày ánh xạ
# kiểu đổi, phép đo đổi theo, không âm thầm đo một hình dạng đã lỗi thời.
BENCH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "bigint"),
    ("event_time", "timestamp with time zone"),
    ("region", "text"),
    ("status", "text"),
    ("amount", "double precision"),
    ("customer_id", "text"),
    ("payload", "text"),
)

# 220 ký tự hex/dòng — cùng con số với `_PAYLOAD_HEX_CHARS` của
# `scripts/measure_write_path.py`, nên byte/dòng của ba phép đo (2c, ĐO 3, cái
# này) là cùng một đại lượng.
PAYLOAD_CHARS = 220

# DDL của bảng THẬT dùng cho `connector_real_table` — CHỈ chạy trên container
# local. Không có đường nào cho DDL này chạm Aiven: `_seed_local_table` chỉ được
# gọi từ nhánh `local`, và connection Aiven mở ở chế độ chỉ-đọc.
_REAL_TABLE_SCHEMA = "probe_read_cost"
_REAL_TABLE_NAME = "read_path"


def _row_expression(*, with_md5: bool) -> sql.Composable:
    """Bảy biểu thức sinh dòng, SERVER-SIDE, không chạm đĩa.

    `with_md5=False` thay hai cột dùng `md5()` bằng HẰNG chuỗi cùng độ dài —
    dùng cho phép trừ định lượng chi phí md5 (nhiễu #2 ở docstring đầu file).
    Độ dài giữ nguyên nên byte Arrow và byte dây không đổi; chỉ CPU server đổi.
    """
    customer_id: sql.Composable
    payload: sql.Composable
    if with_md5:
        customer_id = sql.SQL("'cust-' || substr(md5(i::text), 1, 16)")
        # rpad(<32 hex>, 220, <32 hex khác>): đúng 220 ký tự, 32 ký tự đầu khác
        # nhau từng dòng. Hai lời gọi md5 mỗi dòng thay vì bảy (220/32 = 6,875)
        # — chi phí md5 là NHIỄU của phép đo này, nên giữ nó nhỏ rồi định lượng,
        # chứ không phóng đại nó lên rồi phải trừ một số lớn.
        payload = sql.SQL("rpad(md5(i::text), {}, md5((i * 7 + 13)::text))").format(
            sql.Literal(PAYLOAD_CHARS)
        )
    else:
        customer_id = sql.Literal("cust-" + "0" * 16)
        payload = sql.Literal("a" * PAYLOAD_CHARS)
    return sql.SQL(" ").join(
        [
            sql.SQL("SELECT i::bigint AS id,"),
            sql.SQL("TIMESTAMPTZ '2024-01-01 00:00:00+00' + (i * INTERVAL '1 second')"),
            sql.SQL("AS event_time,"),
            sql.SQL("'region-' || lpad((i % 16)::text, 2, '0') AS region,"),
            sql.SQL("(ARRAY['pending','processing','completed','failed','refunded'])"),
            sql.SQL("[1 + (i % 5)] AS status,"),
            sql.SQL("((i % 2500000)::float8) / 100.0 AS amount,"),
            customer_id,
            sql.SQL("AS customer_id,"),
            payload,
            sql.SQL("AS payload"),
        ]
    )


def generate_series_query(rows: int, *, with_md5: bool = True) -> sql.Composed:
    """Câu SELECT sinh `rows` dòng đúng hình dạng bench, KHÔNG chạm bảng nào."""
    return sql.SQL("{} FROM generate_series(1, {}) AS i").format(
        _row_expression(with_md5=with_md5), sql.Literal(rows)
    )


def bench_column_infos() -> tuple[_ColumnInfo, ...]:
    """`_ColumnInfo` dựng qua CHÍNH `_arrow_type_for`/`_needs_text_cast` của
    production — xem chú thích ở `BENCH_COLUMNS`. `nullable=False` cho cả bảy:
    `generate_series` không sinh NULL nào, và bảng bench của ĐO 3 cũng NOT NULL,
    nên bitmap hợp lệ không xuất hiện ở cả hai bên."""
    return tuple(
        _ColumnInfo(
            name=name,
            arrow_type=_arrow_type_for(pg_type),
            nullable=False,
            text_cast=_needs_text_cast(pg_type),
        )
        for name, pg_type in BENCH_COLUMNS
    )


# ─────────────────────────── bốn (sáu) phép đo ────────────────────────────


@dataclass
class Measured:
    """Kết quả THÔ của một lần đo, chưa quy ra MB/s."""

    seconds: float
    rows: int
    wire_bytes: int = 0
    arrow_bytes: int = 0
    server_ms: float = 0.0


def _copy_path(
    conn: psycopg.Connection[Any], rows: int, *, binary: bool, with_md5: bool = True
) -> Measured:
    """Ô 1 và ô 3 — `COPY (SELECT ...) TO STDOUT`, ĐẾM BYTE DÂY.

    Gần như không có object Python nào được dựng: vòng lặp chỉ cộng độ dài của
    từng khối `memoryview` mà psycopg đưa ra.

    `with_md5=False` là biến thể quan trọng nhất của cả script, không phải một
    tuỳ chọn phụ. `COPY` với `md5()` đo GỘP hai thứ: CPU sinh dòng phía server
    và đường truyền. Biến thể hằng-chuỗi sinh **ĐÚNG CÙNG SỐ BYTE DÂY** (21 ký
    tự `customer_id`, 220 ký tự `payload` — đếm được, in ra ở tổng kết) với chi
    phí sinh gần bằng 0, nên nó là ước lượng SẠCH nhất cho trần ĐƯỜNG TRUYỀN
    thuần. Hiệu hai biến thể là chi phí sinh dòng nhìn XUYÊN QUA cả pipeline
    (khác `EXPLAIN ANALYZE`, thứ chỉ nhìn phía server).
    """
    fmt = sql.SQL(" (FORMAT binary)") if binary else sql.SQL("")
    statement = sql.SQL("COPY ({}) TO STDOUT{}").format(
        generate_series_query(rows, with_md5=with_md5), fmt
    )
    wire = 0
    t0 = time.perf_counter()
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for chunk in copy:
            wire += len(chunk)
    seconds = time.perf_counter() - t0
    return Measured(seconds=seconds, rows=rows, wire_bytes=wire)


def _cursor_drain(conn: psycopg.Connection[Any], rows: int, *, batch_rows: int) -> Measured:
    """Lớp GIỮA — cursor CÓ TÊN + `dict_row` như connector, nhưng KHÔNG dựng
    Arrow. Hiệu với `_copy_path` là chi phí dựng object Python của psycopg;
    hiệu với `_connector_path` là chi phí Arrow thuần."""
    n = 0
    t0 = time.perf_counter()
    with conn.cursor(name="probe_cursor_drain", row_factory=dict_row) as cur:
        cur.itersize = batch_rows
        cur.execute(generate_series_query(rows))
        for _row in cur:
            n += 1
    seconds = time.perf_counter() - t0
    return Measured(seconds=seconds, rows=n)


def _connector_path(
    conn: psycopg.Connection[Any],
    rows: int,
    *,
    batch_rows: int,
    columns: tuple[_ColumnInfo, ...],
) -> Measured:
    """Ô 2 và ô 4 — ĐÚNG vòng lặp của `PostgresConnector._read_rows`.

    Cursor CÓ TÊN (server-side), `dict_row`, `itersize == batch_rows`, gom
    `list[dict]` rồi gọi `_rows_to_record_batch` — hàm được IMPORT từ
    production, không chép lại. Chỉ mệnh đề FROM là khác, và lý do nằm ở
    docstring đầu file.
    """
    arrow_bytes = 0
    n = 0
    t0 = time.perf_counter()
    with conn.cursor(name="loom_connector_read", row_factory=dict_row) as cur:
        cur.itersize = batch_rows
        cur.execute(generate_series_query(rows))
        batch: list[dict[str, object]] = []
        for row in cur:
            batch.append(row)
            if len(batch) >= batch_rows:
                arrow_bytes += _rows_to_record_batch(batch, columns).nbytes
                n += len(batch)
                batch = []
        if batch:
            arrow_bytes += _rows_to_record_batch(batch, columns).nbytes
            n += len(batch)
    seconds = time.perf_counter() - t0
    return Measured(seconds=seconds, rows=n, arrow_bytes=arrow_bytes)


def _explain_path(conn: psycopg.Connection[Any], rows: int, *, with_md5: bool) -> Measured:
    """Nhiễu #1/#2 — CPU SERVER của chính câu SELECT đó, KHÔNG có dây.

    `EXPLAIN (ANALYZE, TIMING OFF)` chạy plan tới cùng và đánh giá đủ target
    list (nên md5/rpad/lpad đều thật sự chạy), nhưng DestReceiver là "none" nên
    không dòng nào được kết xuất ra dạng dây và không byte nào rời server. Vì
    thế nó là CẬN DƯỚI của phần CPU server, không phải con số chính xác — đủ để
    bound một nhiễu, không đủ để trừ thẳng ra khỏi đồng hồ tường, và báo cáo
    phải nói đúng thế.
    """
    statement = sql.SQL("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) {}").format(
        generate_series_query(rows, with_md5=with_md5)
    )
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(statement)
        row = cur.fetchone()
    seconds = time.perf_counter() - t0
    plan: list[dict[str, Any]] = row[0] if row else []
    server_ms = 0.0
    if plan:
        server_ms = float(plan[0].get("Execution Time", 0.0)) + float(
            plan[0].get("Planning Time", 0.0)
        )
    return Measured(seconds=seconds, rows=rows, server_ms=server_ms)


def _connector_real_table(dsn: str, rows: int, *, batch_rows: int) -> Measured:
    """Phép KIỂM CHÉO, CHỈ local — `PostgresConnector` NGUYÊN BẢN trên một BẢNG THẬT.

    Hai việc cùng lúc: (a) chứng minh vòng lặp lắp lại ở `_connector_path` không
    lệch khỏi `_read_rows` thật, (b) định lượng chênh lệch giữa đọc
    `generate_series` và đọc một bảng đã lưu — nhiễu thứ tư ở docstring đầu file.
    """
    connector = PostgresConnector(dsn, batch_rows=batch_rows, schema=_REAL_TABLE_SCHEMA)
    stream = f"{_REAL_TABLE_SCHEMA}.{_REAL_TABLE_NAME}"
    arrow_bytes = 0
    n = 0
    t0 = time.perf_counter()
    for record_batch in connector.read(stream, StreamState()):
        arrow_bytes += record_batch.nbytes
        n += record_batch.num_rows
    seconds = time.perf_counter() - t0
    if n != rows:
        raise RuntimeError(f"bảng thật trả {n} dòng, chờ {rows}")
    return Measured(seconds=seconds, rows=n, arrow_bytes=arrow_bytes)


# ─────────────────────────── trạng thái / nối lại ────────────────────────────

KINDS_WIRE = ("copy_text", "copy_text_nomd5", "copy_binary")
KINDS_CLIENT = ("cursor_drain", "connector")
KINDS_SERVER = ("explain_full", "explain_nomd5")
KINDS_LOCAL_ONLY = ("connector_real_table",)
ALL_KINDS = KINDS_WIRE + KINDS_CLIENT + KINDS_SERVER + KINDS_LOCAL_ONLY


@dataclass
class Sample:
    source: str
    kind: str
    rep: int
    rows: int
    seconds: float
    wire_bytes: int
    arrow_bytes: int
    server_ms: float
    completed_at: str


@dataclass
class Environment:
    """Những gì phải ghi lại để một lần chạy sau ĐỌC ĐƯỢC bối cảnh của lần này."""

    aiven_version: str = ""
    aiven_tls: str = ""
    aiven_db_bytes_before: int = 0
    aiven_db_bytes_after: int = 0
    aiven_read_only: str = ""
    local_version: str = ""
    local_image: str = ""
    cursor_result_format: str = ""
    aiven_rtt_ms: float = 0.0
    local_rtt_ms: float = 0.0


@dataclass
class Progress:
    rows: int
    batch_rows: int
    reps: int
    arrow_bytes_per_row: float
    env: Environment
    samples: list[Sample] = field(default_factory=list)
    peak_rss_kb: int = 0


def save_progress(path: Path, progress: Progress) -> None:
    """Ghi ATOMIC (`.tmp` rồi `rename`) — cùng cơ chế `measure_write_path.py`."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(progress), indent=2, ensure_ascii=False))
    tmp.replace(path)


def load_progress(path: Path) -> Progress:
    raw: dict[str, Any] = json.loads(path.read_text())
    return Progress(
        rows=raw["rows"],
        batch_rows=raw["batch_rows"],
        reps=raw["reps"],
        arrow_bytes_per_row=raw["arrow_bytes_per_row"],
        env=Environment(**raw["env"]),
        samples=[Sample(**s) for s in raw["samples"]],
        peak_rss_kb=raw.get("peak_rss_kb", 0),
    )


class Logger:
    """In ra stdout VÀ ghi file — `tail -f` theo dõi được một lần chạy dài."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")

    def line(self, msg: str) -> None:
        print(msg)
        self._fh.write(msg + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ─────────────────────────── kết nối ────────────────────────────


def read_env_file(path: Path) -> dict[str, str]:
    """Đọc `deploy/local/aiven.env` (gitignore, CREDENTIAL THẬT).

    Giá trị KHÔNG BAO GIỜ được in ra, không đi qua dòng lệnh, không vào
    `progress.json`. Cùng quy ước với target `infra-local-secret` của Makefile.
    """
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
    """Đọc credential từ một thư mục Secret của Kubernetes (CREDENTIAL THẬT).

    Cùng dữ liệu như `read_env_file`, khác HÌNH DẠNG: kubelet chiếu một Secret
    thành MỘT FILE MỖI KHOÁ (`/aiven/username`, `/aiven/password`, ...) chứ
    không phải một file `key=value`. Dùng khi phép đo chạy TRONG CỤM, nơi
    `deploy/local/aiven.env` không tồn tại và Secret `loom-db-app` (do
    `make infra-local-secret` nạp từ chính file đó) là nguồn duy nhất.

    Đọc THẲNG từ file mount vào pod là chủ ý: giá trị không đi qua dòng lệnh
    (`ps` trong pod không thấy), không qua biến môi trường (`kubectl describe
    pod` không thấy), và không vào log. Cùng quy ước "credential không bao giờ
    là một đối số" mà `infra-local-secret` giữ ở phía Makefile.

    `.rstrip("\\n")` chứ không `.strip()`: mật khẩu có thể mở/kết thúc bằng
    khoảng trắng hợp lệ, và `--from-env-file` của kubectl lưu giá trị NGUYÊN
    VĂN — chỉ bỏ đúng ký tự xuống dòng mà trình soạn thảo thêm vào.
    """
    if not path.is_dir():
        raise SystemExit(f"--aiven-secret-dir {path} không phải thư mục (Secret đã mount chưa?)")
    values: dict[str, str] = {}
    for key in ("host", "port", "dbname", "username", "password"):
        item = path / key
        if item.is_file():
            values[key] = item.read_text().rstrip("\n")
    return values


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


def aiven_dsn(env_path: Path, ca_path: Path, secret_dir: Path | None = None) -> str:
    """DSN tới Aiven, MỞ Ở CHẾ ĐỘ CHỈ-ĐỌC.

    `-c default_transaction_read_only=on` là hàng rào THẬT chứ không phải một
    lời hứa trong docstring: mọi `INSERT`/`CREATE`/`COPY FROM` trên connection
    này bị Postgres từ chối, kể cả nếu một thay đổi sau này vô tình thêm một
    câu như thế. Xem docstring đầu file cho lý do (một lần chạy trước đã đẩy
    service này sang chỉ-đọc thật trong lúc control plane đang sống).

    `sslmode=verify-full`: xác thực CẢ hostname, dùng CA của Aiven. Chuỗi trả về
    có mật khẩu — KHÔNG log nó.

    `secret_dir` khác None: lấy credential từ một thư mục Secret đã mount thay
    cho `deploy/local/aiven.env` — đường của bản chạy TRONG CỤM, xem
    `read_secret_dir`. CA cũng nằm trong Secret đó (`ca.pem`), nên `ca_path`
    trỏ vào cùng thư mục.
    """
    if not ca_path.exists():
        raise SystemExit(f"Thiếu {ca_path} — tải CA từ console Aiven (xem aiven.env.example).")
    source: Path = secret_dir if secret_dir is not None else env_path
    env = read_secret_dir(secret_dir) if secret_dir is not None else read_env_file(env_path)
    missing = [k for k in ("host", "port", "dbname", "username", "password") if not env.get(k)]
    if missing:
        raise SystemExit(f"{source} thiếu khoá: {', '.join(missing)}")
    return (
        f"host={env['host']} port={env['port']} dbname={env['dbname']} "
        f"user={env['username']} password={env['password']} "
        f"sslmode=verify-full sslrootcert={ca_path} "
        f"options='-c default_transaction_read_only=on -c statement_timeout=900000'"
    )


def total_database_bytes(conn: psycopg.Connection[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(sum(pg_database_size(datname)), 0)::bigint FROM pg_database")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _scalar(conn: psycopg.Connection[Any], statement: str) -> str:
    with conn.cursor() as cur:
        cur.execute(statement)
        row = cur.fetchone()
    return str(row[0]) if row else "?"


def describe_tls(conn: psycopg.Connection[Any]) -> str:
    """Giao thức + cipher + NÉN của TLS trên chính connection này.

    Đọc `pg_stat_ssl` chứ không hỏi libpq: psycopg 3.3.4 (bản trong workspace
    này — đã kiểm `dir(pq.PGconn)`) chỉ phơi `ssl_in_use`, không có
    `ssl_attribute`.

    KHÔNG chọn cột `compression`, và sự vắng mặt của nó chính là câu trả lời cho
    câu hỏi mà nó lẽ ra dùng để trả lời: PostgreSQL đã BỎ HẲN cột đó khỏi
    `pg_stat_ssl` (đã đo trên chính 17.10 ở đây: `column "compression" does not
    exist`), vì libpq không còn hỗ trợ nén TLS. Nên "byte dây đo được là byte
    THẬT, không phải byte đã nén" không cần một cột để chứng minh — không có
    đường nào cho nén tồn tại.
    """
    if not conn.pgconn.ssl_in_use:
        return "không dùng TLS"
    with conn.cursor() as cur:
        cur.execute("SELECT version, cipher, bits FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        row = cur.fetchone()
    if not row:
        return "TLS bật, pg_stat_ssl không trả dòng nào"
    return f"{row[0]} / {row[1]} / {row[2]} bit (nén: PostgreSQL không còn hỗ trợ)"


def round_trip_ms(conn: psycopg.Connection[Any], samples: int = 25) -> float:
    """Trung vị của một lượt đi-về `SELECT 1` — ĐỘ TRỄ, tách khỏi BĂNG THÔNG.

    Hai đại lượng khác nhau và chỉ một trong hai đặt trần cho phép đo này. Với
    `itersize=100.000`, 500.000 dòng chỉ tốn 5 vòng `FETCH FORWARD`, nên độ trễ
    nhân 5 phải NHỎ so với đồng hồ tường — nếu không thì con số đang đo là số
    vòng chứ không phải băng thông, và báo cáo phải nói thế. In ra để câu đó
    kiểm chứng được thay vì phải tin.
    """
    timings: list[float] = []
    with conn.cursor() as cur:
        for _ in range(samples):
            t0 = time.perf_counter()
            cur.execute("SELECT 1")
            cur.fetchone()
            timings.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(timings)


def cursor_result_format(conn: psycopg.Connection[Any]) -> str:
    """psycopg trả kết quả của cursor CÓ TÊN ở định dạng text hay binary?

    Quan trọng vì cả phép quy "byte dây của `copy_text` ≈ byte dây mà
    `cursor_drain`/`connector` chở" dựa vào câu trả lời. ĐO chứ không nhớ:
    `PGresult.fformat(0)` trả 0 = text, 1 = binary.
    """
    with conn.cursor(name="probe_fmt", row_factory=dict_row) as cur:
        cur.execute(sql.SQL("SELECT 1 AS one"))
        cur.fetchone()
        result = cur.pgresult
        fformat = result.fformat(0) if result is not None else -1
    return {0: "text", 1: "binary"}.get(fformat, f"? ({fformat})")


# ─────────────────────────── nguồn local ────────────────────────────


@contextmanager
def local_postgres(image: str, log: Logger) -> Iterator[str]:
    """Container Postgres local — cùng image `postgres:17-alpine` mà bộ test của
    connectorkit dùng, để mọi Postgres trong repo này là MỘT bản."""
    from testcontainers.community.postgres import PostgresContainer

    log.line(f"Khởi động Postgres local ({image})...")
    with PostgresContainer(image=image) as container:
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )
        yield dsn


def seed_local_table(dsn: str, rows: int, log: Logger) -> None:
    """Bảng THẬT bảy cột trên container LOCAL — chỉ để `connector_real_table`
    có một bảng để `PostgresConnector.read()` đọc. KHÔNG BAO GIỜ chạy trên Aiven
    (xem docstring đầu file); đường gọi duy nhất nằm trong nhánh `local`."""
    log.line(f"Seed bảng thật local {_REAL_TABLE_SCHEMA}.{_REAL_TABLE_NAME} ({rows:,} dòng)...")
    t0 = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_REAL_TABLE_SCHEMA))
        )
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(_REAL_TABLE_SCHEMA)))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.{} ("
                "id bigint NOT NULL, event_time timestamptz NOT NULL, region text NOT NULL, "
                "status text NOT NULL, amount double precision NOT NULL, "
                "customer_id text NOT NULL, payload text NOT NULL)"
            ).format(sql.Identifier(_REAL_TABLE_SCHEMA), sql.Identifier(_REAL_TABLE_NAME))
        )
        cur.execute(
            sql.SQL("INSERT INTO {}.{} {}").format(
                sql.Identifier(_REAL_TABLE_SCHEMA),
                sql.Identifier(_REAL_TABLE_NAME),
                generate_series_query(rows),
            )
        )
        # Bảng NÓNG, không phải nguội — ĐO 3 đo trên một bảng lọt trong cache
        # của Aiven, nên phép so sánh chỉ công bằng nếu bảng này cũng nóng.
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(_REAL_TABLE_SCHEMA), sql.Identifier(_REAL_TABLE_NAME)
            )
        )
    log.line(f"  seed xong sau {time.perf_counter() - t0:.1f}s")


# ─────────────────────────── hiệu chỉnh + vòng chạy ────────────────────────────


def calibrate_bytes_per_row(conn: psycopg.Connection[Any], sample_rows: int) -> float:
    """Byte Arrow trên MỖI DÒNG cho hình dạng này — MẪU SỐ của mọi ô.

    Hiệu chỉnh trên nguồn RẺ (local nếu có), vì biểu thức sinh dòng giống hệt
    nhau ở hai nguồn nên byte/dòng cũng giống hệt. Các lần chạy `connector`
    THẬT vẫn ghi `arrow_bytes` đo được của chính chúng, và tổng kết đối chiếu
    hai con số — lệch nghĩa là giả định trên sai và báo cáo phải nói ra.
    """
    columns = bench_column_infos()
    measured = _connector_path(conn, sample_rows, batch_rows=sample_rows, columns=columns)
    return measured.arrow_bytes / measured.rows


def run_one(
    kind: str,
    *,
    conn: psycopg.Connection[Any] | None,
    dsn: str,
    rows: int,
    batch_rows: int,
    columns: tuple[_ColumnInfo, ...],
) -> Measured:
    if kind in KINDS_WIRE:
        assert conn is not None
        return _copy_path(
            conn,
            rows,
            binary=(kind == "copy_binary"),
            with_md5=(kind != "copy_text_nomd5"),
        )
    if kind == "cursor_drain":
        assert conn is not None
        return _cursor_drain(conn, rows, batch_rows=batch_rows)
    if kind == "connector":
        assert conn is not None
        return _connector_path(conn, rows, batch_rows=batch_rows, columns=columns)
    if kind in ("explain_full", "explain_nomd5"):
        assert conn is not None
        return _explain_path(conn, rows, with_md5=(kind == "explain_full"))
    if kind == "connector_real_table":
        return _connector_real_table(dsn, rows, batch_rows=batch_rows)
    raise ValueError(f"kind lạ: {kind}")


# ─────────────────────────── tổng kết ────────────────────────────


def _stats(values: list[float]) -> tuple[float, float, float, float]:
    """(trung vị, nhỏ nhất, lớn nhất, độ lệch chuẩn mẫu). Độ lệch chuẩn cần ít
    nhất hai mẫu — một mẫu trả 0,0 và cột "tản mát" của báo cáo phải đọc con số
    đó là "chưa đo được", không phải "ổn định tuyệt đối"."""
    if not values:
        return (0.0, 0.0, 0.0, 0.0)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return (statistics.median(values), min(values), max(values), stdev)


def print_summary(progress: Progress, log: Logger) -> None:
    env = progress.env
    bpr = progress.arrow_bytes_per_row
    arrow_mb = progress.rows * bpr / 1e6

    log.line("")
    log.line("=== MÔI TRƯỜNG ===")
    log.line(f"Aiven      : {env.aiven_version}")
    log.line(f"  TLS      : {env.aiven_tls}")
    log.line(f"  chỉ-đọc  : default_transaction_read_only = {env.aiven_read_only}")
    log.line(
        f"  đĩa      : {env.aiven_db_bytes_before:,} byte TRƯỚC -> "
        f"{env.aiven_db_bytes_after:,} byte SAU "
        f"(chênh {env.aiven_db_bytes_after - env.aiven_db_bytes_before:+,})"
    )
    log.line(f"  RTT      : {env.aiven_rtt_ms:.2f} ms (trung vị 25 lượt SELECT 1)")
    log.line(f"Local      : {env.local_version} (image {env.local_image})")
    log.line(f"  RTT      : {env.local_rtt_ms:.2f} ms")
    log.line(f"Định dạng kết quả cursor có tên: {env.cursor_result_format}")
    log.line(
        f"Hình dạng  : {progress.rows:,} dòng x {bpr:.1f} byte Arrow/dòng = "
        f"{arrow_mb:.1f} MB Arrow   (batch_rows={progress.batch_rows:,})"
    )
    log.line(f"RSS đỉnh tiến trình đo: {progress.peak_rss_kb / 1024:.0f} MiB")

    log.line("")
    log.line("=== SỐ ĐO (mẫu số = byte Arrow, ĐÚNG đại lượng của ĐO 3 và 2c) ===")
    header = (
        f"{'nguồn':<6} {'phép đo':<20} {'n':>2} {'trung vị s':>11} "
        f"{'min-max s':>15} {'sd s':>7} {'MB/s Arrow':>11} {'MB/s dây':>10}"
    )
    log.line(header)
    log.line("-" * len(header))
    for source in ("aiven", "local"):
        for kind in ALL_KINDS:
            picked = [s for s in progress.samples if s.source == source and s.kind == kind]
            if not picked:
                continue
            secs = [s.seconds for s in picked]
            median, lo, hi, sd = _stats(secs)
            if kind in KINDS_SERVER:
                server = [s.server_ms / 1000.0 for s in picked]
                m_srv, lo_srv, hi_srv, sd_srv = _stats(server)
                log.line(
                    f"{source:<6} {kind:<20} {len(picked):>2} {m_srv:>11.3f} "
                    f"{lo_srv:>6.3f}-{hi_srv:<8.3f} {sd_srv:>7.3f} "
                    f"{'(CPU server, không có dây)':>22}"
                )
                continue
            arrow_rate = arrow_mb / median if median > 0 else 0.0
            wire = statistics.median([s.wire_bytes for s in picked])
            wire_rate = (wire / 1e6) / median if median > 0 and wire else 0.0
            wire_cell = f"{wire_rate:>10.2f}" if wire_rate else f"{'—':>10}"
            log.line(
                f"{source:<6} {kind:<20} {len(picked):>2} {median:>11.3f} "
                f"{lo:>6.3f}-{hi:<8.3f} {sd:>7.3f} {arrow_rate:>11.2f} {wire_cell}"
            )

    log.line("")
    log.line("=== BYTE DÂY THẬT (trung vị, chỉ các ô COPY) ===")
    for source in ("aiven", "local"):
        for kind in KINDS_WIRE:
            picked = [s for s in progress.samples if s.source == source and s.kind == kind]
            if not picked:
                continue
            wire = statistics.median([s.wire_bytes for s in picked])
            log.line(
                f"{source:<6} {kind:<16} {wire / 1e6:8.1f} MB dây  "
                f"= {wire / progress.rows:6.1f} byte/dòng  "
                f"(byte Arrow/dòng = {bpr:.1f}, tỉ lệ dây/Arrow = {wire / progress.rows / bpr:.3f})"
            )

    _print_split(progress, log)


def _median_seconds(progress: Progress, source: str, kind: str) -> float:
    picked = [s.seconds for s in progress.samples if s.source == source and s.kind == kind]
    return statistics.median(picked) if picked else 0.0


def _print_split(progress: Progress, log: Logger) -> None:
    """Phép TRỪ mà cả script này tồn tại để làm được."""
    arrow_mb = progress.rows * progress.arrow_bytes_per_row / 1e6
    log.line("")
    log.line("=== TÁCH BA LỚP (giây, trung vị) ===")
    for source in ("aiven", "local"):
        copy_s = _median_seconds(progress, source, "copy_text")
        drain_s = _median_seconds(progress, source, "cursor_drain")
        conn_s = _median_seconds(progress, source, "connector")
        if not (copy_s and drain_s and conn_s):
            continue
        log.line(
            f"{source:<6} dây={copy_s:6.2f}s  +psycopg={drain_s - copy_s:+6.2f}s  "
            f"+Arrow={conn_s - drain_s:+6.2f}s  = {conn_s:6.2f}s "
            f"({arrow_mb / conn_s:.2f} MB/s Arrow)"
        )
    log.line("")
    log.line("=== NHIỄU ĐÃ ĐỊNH LƯỢNG: chi phí SINH DÒNG (generate_series + md5) ===")
    for source in ("aiven", "local"):
        full = _median_seconds(progress, source, "copy_text")
        plain = _median_seconds(progress, source, "copy_text_nomd5")
        srv_full = statistics.median(
            [
                s.server_ms
                for s in progress.samples
                if s.source == source and s.kind == "explain_full"
            ]
            or [0.0]
        )
        srv_plain = statistics.median(
            [
                s.server_ms
                for s in progress.samples
                if s.source == source and s.kind == "explain_nomd5"
            ]
            or [0.0]
        )
        if not full:
            continue
        log.line(
            f"{source:<6} COPY có-md5={full:6.2f}s  COPY hằng-chuỗi={plain:6.2f}s  "
            f"chênh={full - plain:+6.2f}s  ({100 * (full - plain) / full:4.1f}% của ô COPY)"
        )
        log.line(
            f"{'':<6} EXPLAIN ANALYZE: đủ={srv_full / 1000:6.2f}s  không-md5="
            f"{srv_plain / 1000:6.2f}s  chênh={(srv_full - srv_plain) / 1000:+6.2f}s "
            "(chỉ CPU server, không có dây)"
        )

    aiven_conn = _median_seconds(progress, "aiven", "connector")
    local_conn = _median_seconds(progress, "local", "connector")
    local_real = _median_seconds(progress, "local", "connector_real_table")
    aiven_copy = _median_seconds(progress, "aiven", "copy_text")
    aiven_wire = _median_seconds(progress, "aiven", "copy_text_nomd5")
    if aiven_conn and local_conn:
        log.line("")
        log.line("=== VERDICT (số thô — diễn giải nằm ở báo cáo) ===")
        log.line(
            f"Aiven qua connector, generate_series : {aiven_conn:6.2f}s = "
            f"{arrow_mb / aiven_conn:5.2f} MB/s Arrow   <- ô so với 7,3 MB/s của ĐO 3"
        )
        log.line(
            f"Local qua connector, generate_series : {local_conn:6.2f}s = "
            f"{arrow_mb / local_conn:5.2f} MB/s Arrow"
        )
        if local_real:
            log.line(
                f"Local qua connector, BẢNG THẬT       : {local_real:6.2f}s = "
                f"{arrow_mb / local_real:5.2f} MB/s Arrow   <- chi phí MÃ, sạch nhiễu sinh dòng"
            )
        if aiven_copy:
            log.line(
                f"Aiven COPY có-md5 (trần dây + sinh)  : {aiven_copy:6.2f}s = "
                f"{arrow_mb / aiven_copy:5.2f} MB/s Arrow-tương-đương"
            )
        if aiven_wire:
            log.line(
                f"Aiven COPY hằng-chuỗi (TRẦN MẠNG)    : {aiven_wire:6.2f}s = "
                f"{arrow_mb / aiven_wire:5.2f} MB/s Arrow-tương-đương  "
                "<- trần cho MỌI cài đặt đọc từ service này"
            )
            log.line("")
            log.line(
                f"Phần đồng hồ tường của ô Aiven mà TRẦN MẠNG đã chiếm: "
                f"{100 * aiven_wire / aiven_conn:5.1f}%"
            )
            if local_real:
                log.line(
                    f"Phần đồng hồ tường của ô Aiven mà MÃ CLIENT chiếm (bảng thật local): "
                    f"{100 * local_real / aiven_conn:5.1f}%"
                )


# ─────────────────────────── CLI ────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rows", type=int, default=500_000, help="Số dòng mỗi phép đo (ĐO 3 dùng 500.000)"
    )
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=100_000,
        help="Dòng mỗi lô Arrow = itersize (ĐO 3 rút trần 7,3 MB/s từ cấu hình C4 = 100.000)",
    )
    parser.add_argument("--reps", type=int, default=3, help="Số lần lặp mỗi ô")
    parser.add_argument("--sources", default="aiven,local")
    parser.add_argument("--kinds", default=",".join(ALL_KINDS))
    parser.add_argument("--calibration-rows", type=int, default=20_000)
    parser.add_argument("--pg-image", default="postgres:17-alpine")
    parser.add_argument("--aiven-env", type=Path, default=REPO_ROOT / "deploy/local/aiven.env")
    parser.add_argument("--aiven-ca", type=Path, default=REPO_ROOT / "deploy/local/aiven-ca.pem")
    parser.add_argument(
        "--aiven-secret-dir",
        type=Path,
        default=None,
        help=(
            "Thư mục Secret Kubernetes đã mount (một file mỗi khoá) thay cho --aiven-env. "
            "Dùng khi chạy TRONG CỤM — xem read_secret_dir()."
        ),
    )
    parser.add_argument(
        "--verify-read-only",
        action="store_true",
        help="CỐ Ý thử CREATE TEMP TABLE/CREATE TABLE trước khi đo, để chứng minh server TỪ CHỐI",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=REPO_ROOT / ".bench-state" / "probe-read-cost"
    )
    parser.add_argument("--fresh", action="store_true", help="Bỏ progress.json cũ, chạy lại từ đầu")
    parser.add_argument(
        "--report-only", action="store_true", help="Chỉ in tổng kết từ progress.json đã lưu"
    )
    return parser.parse_args(argv)


def _peak_rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir: Path = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    progress_path = state_dir / "progress.json"
    log = Logger(state_dir / "run.log")

    try:
        if args.fresh and progress_path.exists():
            progress_path.unlink()
            log.line(f"--fresh: đã xoá {progress_path}")

        if args.report_only:
            if not progress_path.exists():
                log.line(f"Không có {progress_path} — chưa có lần chạy nào để in.")
                return 1
            print_summary(load_progress(progress_path), log)
            return 0

        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        unknown = [k for k in kinds if k not in ALL_KINDS]
        if unknown:
            log.line(f"--kinds có tên lạ: {unknown}. Hợp lệ: {list(ALL_KINDS)}")
            return 1

        columns = bench_column_infos()
        progress = (
            load_progress(progress_path)
            if progress_path.exists()
            else Progress(
                rows=args.rows,
                batch_rows=args.batch_rows,
                reps=args.reps,
                arrow_bytes_per_row=0.0,
                env=Environment(),
            )
        )
        if progress_path.exists():
            log.line(f"Tiếp tục từ {progress_path} ({len(progress.samples)} mẫu đã có).")
            if (progress.rows, progress.batch_rows) != (args.rows, args.batch_rows):
                log.line(
                    f"TỪ CHỐI: progress.json đo {progress.rows:,} dòng / "
                    f"{progress.batch_rows:,} dòng-lô, lần này xin {args.rows:,} / "
                    f"{args.batch_rows:,}. Trộn hai hình dạng vào một tổng kết là "
                    "cách chắc chắn nhất để ra một con số vô nghĩa — dùng --fresh "
                    "hoặc một --state-dir khác."
                )
                return 1
            progress.reps = max(progress.reps, args.reps)

        done = {(s.source, s.kind, s.rep) for s in progress.samples}

        aiven_conn: psycopg.Connection[Any] | None = None
        local_conn: psycopg.Connection[Any] | None = None
        local_dsn = ""

        with (
            local_postgres(args.pg_image, log) if "local" in sources else _null_context()
        ) as maybe_dsn:
            if "local" in sources:
                assert maybe_dsn is not None
                local_dsn = maybe_dsn
                # KHÔNG autocommit — cursor CÓ TÊN đòi một transaction block
                # (`DECLARE CURSOR can only be used in transaction blocks`), và
                # `PostgresConnector` production cũng mở connection ở đúng chế
                # độ này (`psycopg.connect(dsn)` không truyền autocommit).
                local_conn = psycopg.connect(local_dsn)
                progress.env.local_version = _scalar(local_conn, "SELECT version()")[:70]
                progress.env.local_image = args.pg_image
                progress.env.local_rtt_ms = round_trip_ms(local_conn)
                if "connector_real_table" in kinds:
                    seed_local_table(local_dsn, args.rows, log)

            if "aiven" in sources:
                aiven_conn = psycopg.connect(
                    aiven_dsn(args.aiven_env, args.aiven_ca, args.aiven_secret_dir),
                    connect_timeout=20,
                )
                progress.env.aiven_version = _scalar(aiven_conn, "SELECT version()")[:70]
                progress.env.aiven_tls = describe_tls(aiven_conn)
                progress.env.aiven_read_only = _scalar(
                    aiven_conn, "SHOW default_transaction_read_only"
                )
                progress.env.cursor_result_format = cursor_result_format(aiven_conn)
                progress.env.aiven_rtt_ms = round_trip_ms(aiven_conn)
                if not progress.env.aiven_db_bytes_before:
                    progress.env.aiven_db_bytes_before = total_database_bytes(aiven_conn)
                log.line(f"Aiven: {progress.env.aiven_version}")
                log.line(f"  TLS: {progress.env.aiven_tls}")
                log.line(f"  default_transaction_read_only = {progress.env.aiven_read_only}")
                log.line(f"  tổng đĩa mọi database TRƯỚC: {progress.env.aiven_db_bytes_before:,} B")
                if progress.env.aiven_read_only.lower() not in ("on", "true"):
                    log.line("TỪ CHỐI CHẠY: connection Aiven KHÔNG ở chế độ chỉ-đọc.")
                    return 1
                if args.verify_read_only:
                    # TRƯỚC mọi phép đo, không sau: nếu hàng rào hỏng thì điều
                    # cần làm là không đo, chứ không phải phát hiện ra sau khi
                    # đã chạy 7 phút trên một service đang chở control plane.
                    for line in verify_read_only(aiven_conn):
                        log.line(f"  {line}")
            elif local_conn is not None:
                progress.env.cursor_result_format = cursor_result_format(local_conn)

            if not progress.arrow_bytes_per_row:
                calib_conn = local_conn or aiven_conn
                assert calib_conn is not None
                progress.arrow_bytes_per_row = calibrate_bytes_per_row(
                    calib_conn, args.calibration_rows
                )
                calib_conn.rollback()
                log.line(
                    f"Hiệu chỉnh: {progress.arrow_bytes_per_row:.2f} byte Arrow/dòng "
                    f"=> {args.rows:,} dòng = "
                    f"{args.rows * progress.arrow_bytes_per_row / 1e6:.1f} MB Arrow"
                )
                save_progress(progress_path, progress)

            # LẶP-TRƯỚC, không ô-trước: xem docstring đầu file (nhiễu mạng theo đợt).
            for rep in range(progress.reps):
                for source in sources:
                    conn = aiven_conn if source == "aiven" else local_conn
                    for kind in kinds:
                        if kind in KINDS_LOCAL_ONLY and source != "local":
                            continue
                        if (source, kind, rep) in done:
                            continue
                        measured = run_one(
                            kind,
                            conn=conn,
                            dsn=local_dsn,
                            rows=args.rows,
                            batch_rows=args.batch_rows,
                            columns=columns,
                        )
                        progress.samples.append(
                            Sample(
                                source=source,
                                kind=kind,
                                rep=rep,
                                rows=measured.rows,
                                seconds=measured.seconds,
                                wire_bytes=measured.wire_bytes,
                                arrow_bytes=measured.arrow_bytes,
                                server_ms=measured.server_ms,
                                completed_at=datetime.now(UTC).isoformat(),
                            )
                        )
                        # ĐÓNG transaction ngay sau mỗi mẫu. Connection không
                        # autocommit nên mỗi phép đo mở một transaction ngầm, và
                        # để nó treo `idle in transaction` trên Aiven giữ một
                        # snapshot sống — trên một service 1 CPU đang chở control
                        # plane thật, đó là thứ chặn autovacuum, không phải một
                        # chi tiết vệ sinh. `rollback` chứ không `commit`: mọi
                        # thứ ở đây là đọc, và connection Aiven vốn chỉ-đọc.
                        if conn is not None:
                            conn.rollback()
                        progress.peak_rss_kb = max(progress.peak_rss_kb, _peak_rss_kb())
                        save_progress(progress_path, progress)
                        extra = ""
                        if measured.wire_bytes:
                            extra = f" dây={measured.wire_bytes / 1e6:.1f}MB"
                        if measured.arrow_bytes:
                            extra += f" arrow={measured.arrow_bytes / 1e6:.1f}MB"
                        if measured.server_ms:
                            extra += f" cpu_server={measured.server_ms / 1000:.2f}s"
                        log.line(
                            f"[rep {rep}] {source:<6} {kind:<20} "
                            f"{measured.seconds:7.3f}s dòng={measured.rows:,}{extra}"
                        )

            if aiven_conn is not None:
                progress.env.aiven_db_bytes_after = total_database_bytes(aiven_conn)
                save_progress(progress_path, progress)
                log.line(f"tổng đĩa mọi database SAU: {progress.env.aiven_db_bytes_after:,} B")
                aiven_conn.close()
            if local_conn is not None:
                local_conn.close()

        print_summary(progress, log)
        return 0
    finally:
        log.close()


@contextmanager
def _null_context() -> Iterator[str | None]:
    yield None


if __name__ == "__main__":
    raise SystemExit(main())
