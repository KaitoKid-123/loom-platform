"""ĐO 6 của Giai đoạn 3d (CỬA CHẶN cuối) — CHI PHÍ CLIENT trên mỗi MB.

Đây là một PHÉP ĐO, không phải một tính năng. Nó không có người dùng, không có
đường vào từ API, và không có gì trong `services/` gọi tới nó. Cái nó phải cho
ra là những con số THẬT, kể cả khi chúng trượt ngưỡng — nhất là khi chúng trượt.

File này là bản PORT của ĐO 3 sang đường ghi của Giai đoạn 3d. Hai thay đổi lớn,
và cả hai đều đổi ý nghĩa của con số cuối:

  1. **Giai đoạn 3 và 4 đo `write`/`commit`, KHÔNG còn `tx.append`.** Bản trước
     bấm giờ `tx.append` + `commit_transaction()` — đúng API mà `IcebergSink`
     dùng cho tới 3a, và KHÔNG còn là API mà nó dùng. Chạy bản cũ nguyên vẹn trên
     mã hôm nay sẽ đo ĐƯỜNG GHI CŨ và báo một hồi quy KHÔNG TỒN TẠI: đường mới
     ghi Parquet thẳng (`DataFileWriter`) rồi đăng ký cả nhóm bằng MỘT
     `add_files` (`Lakehouse.register_files`), nên một phép đo còn gọi `tx.append`
     vừa bỏ qua phần rẻ vừa tính phần đắt K lần. Đó là hình dạng tệ nhất của một
     phép đo sai: nó chạy xong, in ra một bảng đầy đủ, và nói rằng cả giai đoạn
     không đạt được gì.
  2. **Thước đo là CHI PHÍ CLIENT/MB, không phải MB/s và không phải một tỉ số.**
     Xem mục "thước đo" dưới đây.

## Thước đo — spec 3d mục 2

    chi phí client/MB = (đồng hồ tường lần NẠP - đồng hồ tường `COPY` THÔ) / số MB

`COPY ... TO STDOUT` rút đúng những byte đó ra khỏi cùng bảng nguồn trong CÙNG
lần chạy và không làm gì với chúng, nên nó là mốc "rút byte trần". Phần đường
truyền có mặt ở CẢ HAI vế nên nó TRIỆT TIÊU.

Vì sao không phải MB/s: nó chủ yếu nói về đường truyền của bạn. Vì sao không phải
TỈ SỐ (`nạp / COPY`): tỉ số KHEN MẠNG CHẬM — cùng một đoạn mã đo 1,88x qua Aiven
và 2,19x trong cụm, vì chi phí client trốn được vào lúc chờ mạng. Một thước đo mà
đường truyền tệ đi thì điểm đẹp lên là thước đo hỏng.

Hệ quả tốt: thước này đo được trên Postgres TRONG CỤM, nên không có nhiễu
internet ±8%, không tranh connection slot, và KHÔNG phụ thuộc Aiven cho cửa chặn
cuối của cả giai đoạn.

**N (ngân sách) KHÔNG có mặc định trong file này, và đó là chủ đích.** Ngưỡng cũ
sai HAI lần vì được chốt TRƯỚC khi đo (14,7 MB/s = 60% của một benchmark không
chạm mạng; rồi 6,01 MB/s = 60% của một TRẦN VẬT LÝ, một phép nhân giữ nguyên con
số mà đổi hoàn toàn độ khắt khe). `--budget-ms-per-mb` vì vậy mặc định KHÔNG có
giá trị: thiếu nó, script in số và nói thẳng rằng N chưa chốt. Nó không bịa một
verdict.

## Ba Ô, đo XEN KẼ, mỗi ô ≥3 lần

    copy        `COPY (SELECT ... ORDER BY <cursor>) TO STDOUT`, rút và đếm byte
    add_files   đường nạp HÔM NAY: ghi Parquet + `add_files` mỗi K lô (mã 3d)
    append      đường ghi TRƯỚC 3d: `Lakehouse.append` mỗi lô, K = 1

**Ô `append` chỉ đổi ĐƯỜNG GHI, không đổi cỡ lô — nên nó KHÔNG phải "cấu hình
trước 3d".** Cấu hình đó là `batch_rows = 40.000` VÀ commit từng lô, tức
`--batch-rows 40000 --commit-every 1 --cells copy,append` trong một lần chạy
RIÊNG. Chạy `append` ở cỡ lô mới cho nó hưởng sẵn một nửa phần cải thiện (lô lớn
gấp đôi -> một nửa số lô -> một nửa số commit), nên hiệu số trong CÙNG một lần
chạy đo đúng phần mà `add_files` mua được, và KHÔNG đo phần cả giai đoạn mua
được. Hai câu hỏi khác nhau, hai lần chạy khác nhau; gộp chúng là báo phần cải
thiện nhỏ hơn thật.

`append` là vế ĐỐI CHỨNG, và giới hạn của nó phải nói ra: nó dựng lại hai tính
chất mà con số 3a phụ thuộc vào (một commit catalog mỗi lô, `pa.Table.from_
batches` mỗi lô) chứ không phải mã 3a lịch sử — mã đó không còn tồn tại. Nếu 3a
có một chi tiết đắt nào KHÁC hai điều đó thì vế này đo NHANH HƠN 3a thật, tức
phần cải thiện báo về là CHẶN DƯỚI. Cùng cách và cùng giới hạn như
`_Phase3aSink` ở `scripts/measure_ingest_rss.py`.

`ORDER BY <cursor>` có trong CẢ ba ô: `PostgresConnector._read_rows` thêm nó cho
mọi lần đọc `incremental` (kể cả lần đầu, khi chưa có `cursor_value`), nên một ô
`copy` KHÔNG có `ORDER BY` sẽ rẻ hơn một cách không liên quan gì tới mã client,
và hiệu số sẽ tính phép sắp xếp phía server thành "chi phí client".

XEN KẼ (rep 1 của cả ba ô, rồi rep 2, rồi rep 3) chứ không chạy hết một ô rồi
sang ô khác: một tải đồng thời xuất hiện giữa lần chạy đã có lần làm phồng mẫu
~3x trong dự án này, và với thứ tự xen kẽ thì nó rơi vào CẢ BA ô thay vì chỉ ô
đang chạy. Báo cáo in RA TỪNG MẪU kèm dấu thời gian, không chỉ trung vị và độ
tản, vì đúng lần đó trung vị và độ tản KHÔNG cho thấy gì.

## Năm giai đoạn, và vì sao chúng phải tách

  1. **Đọc nguồn**      — `next()` trên iterator của `PostgresConnector.read`:
                          một vòng `FETCH FORWARD` trên cursor CÓ TÊN, cộng phép
                          dựng `pa.RecordBatch` từ các dòng lấy về.
  2. **Biến đổi**       — `loom_task.runner.add_bronze_columns`: ba cột bronze.
  3. **Ghi Parquet**    — `pa.Table.from_batches` + `DataFileWriter.write`: file
                          Parquet đi thẳng client -> S3, KHÔNG commit. Sau bước
                          này dữ liệu nằm trên S3 và KHÔNG người đọc nào thấy nó.
                          Bao gồm cả MỘT `load_table` cho mỗi NHÓM (chỗ
                          `data_file_writer` mở writer) — production trả đúng giá
                          đó ở đúng chỗ đó, xem `IcebergSink._write`.
  4. **Đăng ký (`add_files`)** — `Lakehouse.register_files`: MỘT commit cho cả
                          nhóm K file. Đây là chỗ ĐO 3 đo 44,0% đồng hồ tường
                          dưới cái tên "commit catalog", và là chỗ cả Giai đoạn
                          3d tồn tại để rút xuống. Nó bằng 0,0s ở những lô KHÔNG
                          đóng nhóm — đó là số đúng, không phải một chỗ thiếu.
  5. **Báo tiến độ**    — `POST /internal/ingest/{run_id}/progress`, MỘT lần cho
                          mỗi NHÓM và SAU khi nhóm đã commit (hợp đồng
                          ghi-trước-báo-sau, spec 3d mục 3b).

Ở đường `append` giai đoạn 3 và 4 KHÔNG TÁCH ĐƯỢC: `Lakehouse.append` nạp bảng,
ghi file và commit trong một lời gọi. Ô đó vì vậy báo cả cục vào giai đoạn 4 và
đặt `stages_separable=False`; bảng tổng kết in ra điều đó chứ không im lặng để
người đọc thấy "ghi Parquet 0,0s" và tự kết luận sai.

## Cái này KHÔNG chạy `loom_task.main.ingest`, và đây là giới hạn của nó

Vòng lặp dưới đây LẮP LẠI `run_incremental`/`run_full` từ chính những mảnh mà
đường thật dùng (`PostgresConnector`, `add_bronze_columns`, `Lakehouse.
data_file_writer`/`register_files`, `staging_table_name`/`old_target_name`), nhưng
nó KHÔNG gọi hai hàm đó. Lý do là bấm giờ: `IcebergSink.write`/`commit` là bề mặt
mà `run_incremental` gọi, và bên trong chúng phần "ghi file" với phần "một
`load_table`" nằm cùng một lời gọi — tách chúng chính là cả điểm của phép đo này.

Giá phải trả, nói thẳng: một thay đổi trong `runner.py` hoặc `sink.py` KHÔNG tự
động chảy vào đây, và bản trước của file này đã chứng minh giá đó là thật — nó
sống sót qua việc `Sink` đổi từ `append` sang `write`/`commit` mà không một dòng
nào đỏ. Cái làm rủi ro đó nhỏ là mọi mảnh ĐẮT đều được import chứ không chép lại;
chỉ THỨ TỰ gọi là viết lại, và thứ tự đó được `services/loom-task/tests` canh
riêng. Cái làm rủi ro đó KHÔNG bằng không là không có phép canh nào nối hai chỗ.

## Nguồn: Postgres TRONG CỤM là mặc định, Aiven là một lựa chọn CÒN LẠI

`--source-kind bench` (mặc định) đọc bảng do `make bench-source-up` dựng: một
Postgres dùng-một-lần trong cụm, `emptyDir`, mật khẩu sinh trong cụm, bảy cột
đúng hình dạng bảng bench của ĐO 3/ĐO 4 (`scripts/bench_source.sql`). Không một
byte nào tới Aiven.

`--source-kind aiven` giữ lại đường cũ, CHỈ-ĐỌC qua `_aiven_guard`. Nó đòi một
bảng ĐÃ CÓ SẴN và không tạo gì: đường ghi lên Aiven đã bị gỡ hẳn sau khi một lần
nạp 1,2 triệu dòng lấp đầy đĩa của service gói 1 GB và lật CẢ service sang
chỉ-đọc trong lúc control plane của Loom đang sống trên đó — ngay cả `DROP SCHEMA`
dọn dẹp cũng bị từ chối. Chi tiết ở docstring đầu `_aiven_guard.py`.

Hai hình dạng DSN KHÔNG BAO GIỜ gặp nhau trong mã: nhánh `aiven` mở qua
`_aiven_guard.read_only_connection` (chỗ ĐỌC LẠI `SHOW default_transaction_read_
only` từ chính server), nhánh `bench` tự dựng DSN của nó rồi mở bằng
`psycopg.connect`. Xem `_open_source` — nó cố ý `return` sớm để không có đường
nào cho một DSN Aiven đi tới `psycopg.connect`.

## Mỗi mẫu chạy trong một TIẾN TRÌNH CON — `ru_maxrss` không bao giờ giảm

Ba ô nối tiếp trong một tiến trình thì ô chạy SAU thừa hưởng đỉnh RSS của ô chạy
TRƯỚC, nên nó KHÔNG THỂ đọc thấp hơn. `fork` mỗi mẫu, con tự đọc `ru_maxrss` của
CHÍNH NÓ rồi gửi về qua pipe, và nền lúc fork (`rss_at_fork_mib`) được IN RA cạnh
đỉnh để người đọc trừ được chứ không phải tin. Cùng cách và cùng lý do như
`scripts/measure_ingest_rss.py`. Nó còn giải một chuyện thứ hai: `RestCatalog`
giữ một session HTTP với socket đang mở, và hai tiến trình cùng ghi lên một socket
TCP thì hỏng theo cách rất khó đọc — nên mỗi con tự dựng catalog của nó.

## Không có số nào được PHÁT LẠI mà không nói ra

`--state-dir` bật cơ chế nối lại: mỗi mẫu ghi vào `progress.json` ngay khi xong,
và một lần chạy sau bỏ qua những mẫu đã có. Điều đó tiện, và nó có một lối hỏng
ĐÃ XẢY RA THẬT trong dự án này: `probe_read_path_cost.py` in lại một bảng tổng
kết ĐẦY ĐỦ từ `progress.json` mà không một dấu hiệu nào cho biết lần chạy đó
KHÔNG ĐO GÌ, và những con số cũ đó suýt được báo cáo như một mốc nền mới.

Nên ở đây: mỗi mẫu mang dấu thời gian của lúc nó ĐƯỢC ĐO, bảng tổng kết có cột
`nguồn mẫu` (`đo lần này` / `PHÁT LẠI` / `TRỘN n/m`), và một lần chạy không đo gì
in một dòng chữ hoa nói đúng điều đó TRƯỚC mọi con số. Một ô TRỘN là ô nguy hiểm
nhất — trung vị của nó gộp hai điều kiện máy khác nhau — nên nó được gọi tên
riêng thay vì gộp vào "có phát lại".

KHÔNG có `--state-dir` thì không có cache nào cả, và bảng tổng kết nói thế. Đó là
cấu hình dùng khi chạy trong một Job: pod không có volume bền, nên một `progress.
json` trong lớp container sẽ biến mất cùng pod và cơ chế nối lại không mua được
gì.

## Dọn dẹp — hai phía, và cả hai đều bắt buộc

Phía Iceberg: bỏ đăng ký bảng + namespace + xoá warehouse KHÔNG giải phóng đĩa —
2c đã đo điều đó trên Lakekeeper v0.9.2 (xem `Lakehouse.drop_table`). Chỉ
`purge_s3_prefix` (xoá thẳng qua S3 API) mới thật sự trả lại đĩa, và bỏ qua nó
nghĩa là mỗi lần chạy để lại vài trăm MB Parquet vĩnh viễn trên đĩa host.

Phía nguồn: KHÔNG có gì để dọn. Phép đo này không ghi một dòng nào vào nguồn, ở
cả hai `--source-kind`. Bảng bench trong cụm được `make bench-source-down` xoá
cùng deployment (`emptyDir`), và bảng trên Aiven không do phép đo này dựng.

Chạy: `make bench-source-up` rồi `make measure-client-cost`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from resource import RUSAGE_SELF, getrusage
from typing import TYPE_CHECKING, Any

# CÙNG thư mục, nạp vào cụm qua CÙNG ConfigMap (xem target `measure-client-cost`):
# `python /scripts/measure_ingest_path.py` đặt `/scripts` làm `sys.path[0]`.
import _aiven_guard
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
from loom_task.config import ReadTuning, WriteTuning
from loom_task.runner import add_bronze_columns, resolve_cursor
from loom_task.sink import old_target_name, staging_table_name

if TYPE_CHECKING:
    # `boto3-stubs` là dev dependency, và image `loom/task` build bằng
    # `uv sync --frozen --no-dev` nên KHÔNG có `mypy_boto3_s3` lúc chạy trong
    # pod — đã kiểm thật, cùng cạm bẫy đã ghi ở `scripts/measure_ingest_pod.py`.
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

# Ba Ô. `copy` PHẢI có mặt trong mọi lần chạy đo chi phí client: nó là mẫu số của
# phép trừ, và không có nó thì hiệu số không tồn tại (chứ không phải bằng 0).
CELL_COPY = "copy"
CELL_ADD_FILES = "add_files"
CELL_APPEND = "append"
ALL_CELLS = (CELL_COPY, CELL_ADD_FILES, CELL_APPEND)

# Mốc thời gian cơ sở của bảng "thế hệ trước" mà `--mode full` dựng để cú tráo
# chạy đủ bốn bước. KHÔNG dùng `infinity` hay một khoảng năm quá rộng: psycopg ném
# `DataError: timestamp too large` TRƯỚC khi pyarrow thấy giá trị.
_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def rss_mib() -> float:
    """Đỉnh RSS của CHÍNH tiến trình này, MiB. `ru_maxrss` đếm KiB trên Linux."""
    return float(getrusage(RUSAGE_SELF).ru_maxrss) / 1024


# ─────────────────────────── nguồn: CHỈ ĐỌC, không bao giờ ghi ───────────────────────────
#
# Bản trước của file này DỰNG bảng nguồn: `_SOURCE_DDL`, `_source_rows`,
# `seed_source` (một `COPY ... FROM STDIN` 500.000 dòng) và `drop_source`. Cả
# khối đó đã bị GỠ, và đây là lý do — không phải để gọn.
#
# Chính đường ghi ấy đã lấp đầy đĩa của service Aiven và lật CẢ service sang
# chỉ-đọc trong lúc control plane của Loom đang sống trên đó; ngay cả lệnh
# `DROP SCHEMA` dọn dẹp cũng bị từ chối, tức phép đo tự nhốt mình.
#
# Hàng rào bây giờ là một TÍNH CHẤT KIỂM ĐƯỢC chứ không phải một quy ước: mọi
# connection Aiven trong `scripts/` đi qua `_aiven_guard`, và DSN nó dựng LUÔN
# mang `-c default_transaction_read_only=on`. Không có tham số nào tắt được nó.
# `packages/connectorkit/tests/test_aiven_measurement_guard.py` canh điều đó.


@dataclass(frozen=True)
class Source:
    """Nguồn đã chốt cho cả lần chạy: cách mở nó, và bảng nào trong nó."""

    kind: str
    schema: str
    table: str


def _aiven_dsn_from_env() -> str:
    """DSN Aiven CHỈ-ĐỌC, ghép từ `BENCH_PG_*` do `secretKeyRef` tiêm vào pod.

    KHÔNG nhận DSN qua dòng lệnh: một chuỗi kết nối trên `argv` lộ ra trong `ps`,
    trong log của `kubectl create job`, và trong shell history.

    Việc ghép chuỗi nằm ở `_aiven_guard.dsn_from_environ` — không phải để gọn, mà
    để `-c default_transaction_read_only=on` chỉ có MỘT định nghĩa trong repo.
    """
    return _aiven_guard.dsn_from_environ()


def _bench_dsn_from_environ() -> str:
    """DSN tới Postgres nguồn TRONG CỤM, ghép từ các mảnh trong Secret `bench-source`.

    Ghép Ở ĐÂY chứ không nhận một DSN đủ: một chuỗi có mật khẩu trong `env` của
    Job là một chuỗi hiện ra trong `kubectl describe`. Cùng cách và cùng lý do như
    `scripts/measure_ingest_rss.py`.

    KHÔNG đi qua `_aiven_guard`: đây không phải Aiven. Nó là một Postgres
    dùng-một-lần với `emptyDir`, không chia đĩa với ai, và bị xoá cùng deployment
    — không có control plane nào sống trên nó để bảo vệ. Hàng rào chỉ-đọc vẫn
    không cần thiết vì phép đo này không có một câu ghi nào, ở bất kỳ nhánh nào.
    """
    host = os.environ.get("BENCH_SOURCE_HOST", "bench-pg")
    port = os.environ.get("BENCH_SOURCE_PORT", "5432")
    dbname = os.environ.get("BENCH_SOURCE_DBNAME", "bench")
    user = os.environ.get("BENCH_SOURCE_USER", "bench")
    password = os.environ["BENCH_SOURCE_PASSWORD"]
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def connector_dsn(source: Source) -> str:
    """DSN mà `PostgresConnector` sẽ mở. Nhánh Aiven mang tham số chỉ-đọc sẵn."""
    if source.kind == "aiven":
        return _aiven_dsn_from_env()
    return _bench_dsn_from_environ()


@contextmanager
def _open_source(source: Source) -> Iterator[psycopg.Connection[Any]]:
    """Mở nguồn để chạy SQL tay (đếm dòng, `COPY ... TO STDOUT`).

    Hai nhánh, và nhánh Aiven `return` NGAY sau khối `with` của nó: đó là điều làm
    cho không có đường nào cho một DSN Aiven đi tới `psycopg.connect`. Nhánh
    `bench` tự dựng DSN của chính nó, nên hai chuỗi không bao giờ nằm trong cùng
    một biến.

    `connect_read_only` (chứ không chỉ một DSN có tham số) vì một `options` sai
    chính tả vẫn cho connect thành công và im lặng bỏ qua tham số — chỉ câu `SHOW`
    mà nó chạy mới nói server thực sự đang ở chế độ nào.
    """
    if source.kind == "aiven":
        with _aiven_guard.read_only_connection(_aiven_dsn_from_env(), verify=True) as conn:
            yield conn
        return
    bench_dsn = _bench_dsn_from_environ()
    with psycopg.connect(bench_dsn, connect_timeout=20) as conn:
        yield conn


def require_source_table(source: Source) -> tuple[int, int]:
    """Đòi bảng nguồn ĐÃ CÓ. Trả `(số dòng, byte trên đĩa)`. KHÔNG tạo gì.

    Không có nhánh nào ở đây đi tới `CREATE`, ở cả hai `--source-kind`: nếu bảng
    thiếu thì thứ duy nhất xảy ra là một thông báo kèm cách dựng nó.
    """
    qualified = sql.Identifier(source.schema, source.table)
    with _open_source(source) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
            (source.schema, source.table),
        )
        if cur.fetchone() is None:
            raise SystemExit(_missing_source_message(source))
        cur.execute(
            sql.SQL("SELECT count(*), pg_total_relation_size({}) FROM {}").format(
                sql.Literal(f"{source.schema}.{source.table}"), qualified
            )
        )
        found = cur.fetchone()
        assert found is not None  # `SELECT count(*)` luôn trả đúng một dòng
    return int(found[0]), int(found[1])


def _missing_source_message(source: Source) -> str:
    if source.kind == "bench":
        return (
            f"KHÔNG CÓ bảng nguồn {source.schema}.{source.table} trong Postgres của cụm.\n"
            "Chạy `make bench-source-up` — nó dựng một Postgres dùng-một-lần và seed "
            "500.000 dòng (~175 MB) vào `emptyDir`, không chạm Aiven."
        )
    return (
        f"KHÔNG CÓ bảng nguồn {source.schema}.{source.table} trên Aiven, và script này "
        "KHÔNG tạo nó.\n"
        "Đường ghi đã bị gỡ sau khi một lần nạp lật cả service Aiven sang chỉ-đọc giữa "
        "lúc control plane đang sống (xem _aiven_guard.py).\n"
        "Muốn đo trên Aiven: tự dựng bảng nguồn, CÓ CHỦ Ý, với console Aiven đang mở để "
        "nhìn dung lượng gói. Hoặc dùng `--source-kind bench` — spec 3d mục 2 nói rõ "
        "thước đo chi phí client/MB KHÔNG cần Aiven, vì phần đường truyền triệt tiêu."
    )


# ─────────────────────────── Ô 1: `COPY ... TO STDOUT` thô ────────────────────────────


def copy_to_stdout_cost(source: Source, columns: list[str], order_by: str) -> dict[str, Any]:
    """Rút cả bảng qua `COPY ... TO STDOUT` và ĐẾM byte. Không parse, không giữ.

    Đây là mốc "rút byte trần" của phép trừ. Nó phải đọc ĐÚNG những cột mà đường
    nạp đọc và theo ĐÚNG thứ tự mà đường nạp bắt server sắp xếp, nếu không hiệu số
    sẽ tính phần chênh đó thành "chi phí client".

    Byte đếm được ở đây là byte TRÊN DÂY của định dạng text của `COPY` — KHÔNG
    phải byte Arrow. Hai đại lượng khác nhau, và mẫu số của thước đo là byte Arrow
    (cùng đại lượng ĐO 3 và 2c dùng), nên con số này chỉ đi vào báo cáo như một
    dữ kiện phụ, không vào phép chia.

    Không giữ block nào lại: `for block in copy` rồi cộng độ dài. Gom chúng vào
    một `bytes` sẽ đo thêm chi phí cấp phát vài trăm MB mà đường nạp không trả.
    """
    query = sql.SQL("COPY (SELECT {cols} FROM {table} ORDER BY {order}) TO STDOUT").format(
        cols=sql.SQL(", ").join(sql.Identifier(name) for name in columns),
        table=sql.Identifier(source.schema, source.table),
        order=sql.Identifier(order_by),
    )
    wire_bytes = 0
    with _open_source(source) as conn, conn.cursor() as cur:
        started = time.perf_counter()
        with cur.copy(query) as copy:
            for block in copy:
                wire_bytes += len(block)
        seconds = time.perf_counter() - started
        # `rowcount` đến từ command tag của `COPY`, không từ việc đếm dòng phía
        # client — nên nó không cộng CPU nào vào đồng hồ ở trên. Nó có mặt để bảng
        # tổng kết KIỂM ĐƯỢC rằng ô này rút đúng ngần ấy dòng mà ô nạp đọc: một
        # phép trừ giữa hai ô đọc hai lượng dữ liệu khác nhau là một con số vô
        # nghĩa trông hoàn toàn hợp lệ.
        rows = max(cur.rowcount, 0)
    return {
        "seconds": seconds,
        "wire_bytes": wire_bytes,
        # `arrow_bytes = 0` là thứ loại ô này khỏi việc TÍNH mẫu số MB (nó không
        # dựng một `RecordBatch` nào), không phải một chỗ chưa đo.
        "arrow_bytes": 0,
        "rows": rows,
        "stages_separable": False,
        "table_setup_s": 0.0,
        "batches": [],
        "swap": None,
    }


# ─────────────────────────── báo tiến độ ────────────────────────────


class ProgressReporter:
    """`POST /internal/ingest/{run_id}/progress` với ĐÚNG thân request của pod thật.

    Dựng `IngestProgressReport` chứ không một `dict` tự lắp, cùng lý do
    `loom_task.client.IngestClient` làm thế: hai phép kiểm ở biên của model (ba
    trường cursor đi cùng nhau, giá trị đọc được dưới kiểu nó khai) phải chạy ở
    BÊN GỬI. Nếu phép đo gửi một hình dạng mà pod thật không gửi được, con số nó
    đo là con số của một đường không tồn tại.

    KHÔNG dùng thẳng `IngestClient`: lớp đó gọi `GET .../spec` để lấy `source_id`,
    và lời gọi đó CHUYỂN TRẠNG THÁI của run (`pending` -> `running`). Một phép đo
    không được phép làm đổi trạng thái một hàng dữ liệu thật chỉ để lấy một con số.

    `run_id` phải là một hàng `ingest_run` CÓ THẬT — handler trả 404 cho id lạ, và
    một phép đo bấm giờ đường 404 sẽ báo một con số nhỏ hơn thật rất nhiều (không
    có `_advance_watermark` nào chạy).

    **`cursor_offset` cộng vào giá trị watermark GỬI ĐI, và chỉ vào đó.** Nó tồn
    tại vì một cạm bẫy đo lường có thật: watermark của một stream được LƯU LẠI,
    nên mẫu thứ HAI trên cùng bảng báo về những giá trị THẤP HƠN mốc mà mẫu thứ
    nhất để lại, `moves_forward` trả `False`, và `_advance_watermark` bỏ hẳn câu
    `UPDATE`. Mẫu thứ hai khi đó đo một nhánh RẺ HƠN nhánh mà mẫu thứ nhất đã đo
    — hai con số trông so được với nhau nhưng không phải.
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

    Cần một phép đo riêng chứ không đọc ra được từ vòng lặp chính: chênh lệch
    giữa hai lần chạy trộn lẫn chi phí watermark với nhiễu của mạng ở hai lúc khác
    nhau. So le `rows`-một-mình với `rows`+cursor trong CÙNG một vòng lặp làm cả
    hai hình dạng gặp cùng một điều kiện mạng.

    Lấy TRUNG VỊ chứ không trung bình: một cú GC hay một checkpoint kéo trung bình
    đi rất xa, còn trung vị thì không.

    `cursor_value` TĂNG NGHIÊM NGẶT qua từng cặp, và đó là điều kiện để phép đo có
    nghĩa: `_advance_watermark` chỉ chạy `UPDATE` khi `moves_forward` đúng, nên một
    giá trị đứng yên sẽ đo nhánh RẺ (chỉ đọc, không ghi).

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


# ─────────────────────────── dọn S3 ────────────────────────────


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
    parquet_write_s: float
    register_s: float
    progress_s: float


@dataclass
class SwapRecord:
    """Chi phí CỐ ĐỊNH của `full` — một lần mỗi lần chạy, KHÔNG co giãn theo số lô.

    `purge_s3_s` tách riêng vì đường nạp thật **KHÔNG** chạy nó:
    `IcebergSink.drop_old_target` chỉ bỏ TÊN khỏi catalog, còn object trên S3 ở
    lại (nợ có tên ở spec 3a mục 13). Đo nó ở đây để biết món nợ đó đáng bao nhiêu,
    chứ không để nói rằng nó đã được trả.
    """

    staging_done_s: float
    target_exists_s: float
    rename_target_away_s: float
    promote_staging_s: float
    drop_old_target_s: float
    purge_s3_s: float
    purged_objects: int
    had_target: bool


@dataclass
class Sample:
    """MỘT lần đo MỘT ô. `measured_at` là thứ làm một mẫu phát lại nhận ra được."""

    cell: str
    rep: int
    measured_at: str
    seconds: float
    rows: int
    arrow_bytes: int
    wire_bytes: int
    stages_separable: bool
    table_setup_s: float
    peak_rss_mib: float
    rss_at_fork_mib: float
    batches: list[BatchRecord] = field(default_factory=list)
    swap: SwapRecord | None = None
    error: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.cell, self.rep)

    def stage_totals(self) -> dict[str, float]:
        return {
            "read_s": sum(b.read_s for b in self.batches),
            "transform_s": sum(b.transform_s for b in self.batches),
            "parquet_write_s": sum(b.parquet_write_s for b in self.batches),
            "register_s": sum(b.register_s for b in self.batches),
            "progress_s": sum(b.progress_s for b in self.batches),
        }


def sample_from_dict(raw: dict[str, Any]) -> Sample:
    """Dựng lại một `Sample` từ `progress.json`. Dựng cả `BatchRecord` con.

    `dataclass(**dict)` KHÔNG tự dựng dataclass lồng nhau, nên bỏ hai dòng dưới
    đây sẽ cho ra một `Sample` mà `batches` là list of dict — và
    `sum(b.read_s ...)` sẽ ném `AttributeError` ở giữa bảng tổng kết, sau khi phép
    đo đã chạy xong.
    """
    data = dict(raw)
    data["batches"] = [BatchRecord(**b) for b in data.get("batches") or []]
    swap = data.get("swap")
    data["swap"] = SwapRecord(**swap) if swap else None
    return Sample(**data)


# ─────────────────────────── nối lại, và dấu PHÁT LẠI ────────────────────────────


@dataclass
class Progress:
    """Trạng thái nối lại được. `fingerprint` là thứ chặn việc trộn hai lần đo khác nhau."""

    fingerprint: dict[str, Any]
    samples: list[Sample] = field(default_factory=list)


def load_progress(path: Path) -> Progress:
    raw = json.loads(path.read_text())
    return Progress(
        fingerprint=raw["fingerprint"],
        samples=[sample_from_dict(s) for s in raw["samples"]],
    )


def save_progress(path: Path, progress: Progress) -> None:
    """Ghi ATOMIC (`.tmp` rồi `rename`): một lần chạy bị cắt giữa lúc ghi không
    được để lại một `progress.json` cắt dở — thứ đó không đọc lại được, nên nó
    biến một cơ chế nối lại thành một cơ chế mất hết."""
    payload = {
        "fingerprint": progress.fingerprint,
        "samples": [asdict(s) for s in progress.samples],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    tmp.rename(path)


def _fingerprint(args: argparse.Namespace, source_rows: int) -> dict[str, Any]:
    """Những gì phải KHỚP để hai mẫu so được với nhau.

    Không gồm `--reps` (thêm rep là đúng cách nối lại) và không gồm `--cells`
    (thêm một ô là hợp lệ). Gồm mọi thứ đổi ý nghĩa của một con số: nguồn, số
    dòng, cỡ lô, K, mode.
    """
    return {
        "source_kind": args.source_kind,
        "source": f"{args.source_schema}.{args.source_table}",
        "source_rows": source_rows,
        "batch_rows": args.batch_rows,
        "commit_every": args.commit_every,
        "mode": args.mode,
    }


# ─────────────────────────── một lần nạp, tách giai đoạn ────────────────────────────


def _seed_previous_generation(lakehouse: Lakehouse, target: str, source_id: str) -> None:
    """Dựng một "thế hệ trước" của bảng đích để cú tráo của `full` chạy đủ bốn bước.

    Lần nạp `full` ĐẦU TIÊN của một lakehouse không có bảng đích, nên cú tráo của
    nó bỏ qua `rename_target_away` và `drop_old_target` — rẻ hơn hẳn trạng thái
    ỔN ĐỊNH. Chi phí cố định cần biết là chi phí của lần nạp thứ HAI trở đi.

    Nằm NGOÀI đồng hồ đo: nó là thiết lập, không phải một giai đoạn.
    """
    seed = pa.table(
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
    lakehouse.create_from(target, seed)


def ingest_cost(
    *,
    cell: str,
    args: argparse.Namespace,
    source: Source,
    catalog: RestCatalog,
    target: str,
    reporter: ProgressReporter | None,
) -> dict[str, Any]:
    """Một lần nạp đầy đủ, năm giai đoạn tách riêng. Trả dict cho `Sample`.

    `cell == CELL_ADD_FILES` là mã HÔM NAY (`DataFileWriter` + `register_files`,
    nhóm K lô); `cell == CELL_APPEND` là vế đối chứng trước 3d (`Lakehouse.append`
    mỗi lô, K = 1). Một hàm cho cả hai, có chủ đích: mọi thứ NGOÀI hai bước ghi
    phải giống nhau từng dòng, nếu không hiệu số giữa hai ô sẽ mang theo cả
    những khác biệt không ai muốn đo.
    """
    per_batch_commit = cell == CELL_APPEND
    if per_batch_commit:
        commit_every = 1
    elif args.mode == "full":
        # `full` KHÔNG bị ràng buộc watermark (spec 3d mục 3b), nên `run_full` dùng
        # K = TẤT CẢ: một `staging_done()` duy nhất ở cuối. Ép K của `incremental`
        # lên đường đó sẽ đo một hình dạng mà production không chạy — số commit
        # nhiều hơn thật, và ô `full` báo chậm hơn thật.
        commit_every = sys.maxsize
    else:
        commit_every = args.commit_every

    lakehouse = Lakehouse(catalog)
    connector = PostgresConnector(
        connector_dsn(source), batch_rows=args.batch_rows, schema=source.schema
    )
    check = connector.check()
    if not check.ok:
        raise SystemExit(f"nguồn không nối được: {check.message}")

    stream = f"{source.schema}.{source.table}"
    # `discover()` MỘT lần, đúng như `loom_task.main.ingest` làm — và cursor lấy từ
    # chính `resolve_cursor` của đường thật, không tự đoán tên cột.
    cursor = resolve_cursor(connector.discover(), stream, None)

    run_id = uuid.uuid4()
    source_id = str(run_id)
    staging = staging_table_name(target, run_id)
    old_target = old_target_name(target, run_id)
    # `full` ghi vào staging, `incremental` ghi thẳng vào đích — đúng hợp đồng của
    # hai vòng lặp thật (`loom_task.runner`).
    write_into = staging if args.mode == "full" else target

    # `full` đọc lại từ ĐẦU (StreamState rỗng). `incremental` ở đây cũng đọc lại từ
    # đầu, và đó là CHỦ ĐÍCH: mọi ô phải chạy trên CÙNG dữ liệu để so được. Truyền
    # `cursor_column` mà không truyền `cursor_value` tái hiện đúng lần nạp
    # `incremental` ĐẦU TIÊN của một stream — connector thêm `ORDER BY <cursor>`,
    # một chi phí THẬT mà ô `copy` cũng được cho trả (xem `copy_to_stdout_cost`).
    state = StreamState() if args.mode == "full" else StreamState(cursor_column=cursor.name)

    if args.mode == "full" and args.pre_create_target:
        _seed_previous_generation(lakehouse, target, source_id)

    batches: list[BatchRecord] = []
    swap: SwapRecord | None = None
    table_setup_s = 0.0
    pending: list[str] = []
    writer: Any = None
    created = False
    files_written = 0
    group_batches = 0
    group_rows = 0
    group_cursor: int | None = None

    def _register_and_report() -> tuple[float, float]:
        """Đăng ký nhóm rồi BÁO — theo đúng thứ tự đó, và chỉ theo thứ tự đó.

        Đảo lại là mất dòng im lặng (`runner.run_incremental`), nên một phép đo
        đảo nó sẽ đo một đường mà production không có quyền chạy.
        """
        nonlocal pending, writer, group_batches, group_rows, group_cursor
        t0 = time.perf_counter()
        lakehouse.register_files(write_into, pending)
        register_s = time.perf_counter() - t0
        pending = []
        # Bỏ writer đi sau mỗi commit — cùng lý do `IcebergSink.commit` làm thế:
        # nó giữ credential STS mà Lakekeeper vend, và credential đó có hạn.
        writer = None

        t0 = time.perf_counter()
        _report(group_rows, group_cursor)
        progress_s = time.perf_counter() - t0

        group_batches = 0
        group_rows = 0
        group_cursor = None
        return register_s, progress_s

    def _report(rows: int, cursor_value: int | None) -> None:
        if reporter is None:
            return
        if args.mode == "full":
            reporter.report(rows=rows)
            return
        assert cursor_value is not None
        reporter.report(
            rows=rows,
            cursor_column=cursor.name,
            cursor_type=cursor.cursor_type,
            cursor_value=cursor_value,
        )

    reader = connector.read(stream, state)
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

        parquet_write_s = 0.0
        register_s = 0.0
        progress_s = 0.0

        # Mốc cursor tính TRƯỚC mọi đồng hồ giai đoạn, và ở CẢ HAI đường. Bản đầu
        # của bản port này tính nó bên trong đồng hồ "báo tiến độ" của đường
        # `append` (nó là một tham số của lời gọi báo) nhưng bên ngoài mọi đồng hồ
        # của đường `add_files` — 0,03 s mỗi lô ở một vế và 0 ở vế kia, tức phần
        # cải thiện báo về nhỏ hơn thật khoảng 0,2 s trên bảy lô. Một phép đo so
        # hai đường phải trả CÙNG một hoá đơn ở mọi chỗ không phải chỗ nó đang so.
        batch_cursor = _cursor_of(batch, cursor.name)

        if per_batch_commit:
            # Đường TRƯỚC 3d: nạp bảng + ghi file + commit trong MỘT lời gọi, nên
            # giai đoạn 3 và 4 không tách được. Cả cục vào giai đoạn 4 (chỗ ĐO 3
            # gọi là "commit catalog"), và `stages_separable=False` nói ra điều đó.
            t0 = time.perf_counter()
            data = pa.Table.from_batches([enriched])
            if not created:
                lakehouse.create_namespace_if_not_exists(write_into.rpartition(".")[0])
                created = True
                if not lakehouse.exists(write_into):
                    lakehouse.create_from(write_into, data)
                else:
                    lakehouse.append(write_into, data)
            else:
                lakehouse.append(write_into, data)
            register_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            _report(batch.num_rows, batch_cursor)
            progress_s = time.perf_counter() - t0
        else:
            if not created:
                # Bảng RỖNG trước file đầu tiên: `DataFileWriter` dựng đường dẫn từ
                # `table.location()`, nên bảng phải có mặt TRƯỚC. Nằm ngoài năm giai
                # đoạn — nó xảy ra đúng một lần và không co giãn theo dữ liệu — nhưng
                # TRONG đồng hồ tường, vì đường nạp thật cũng trả nó.
                t0 = time.perf_counter()
                lakehouse.create_namespace_if_not_exists(write_into.rpartition(".")[0])
                if not lakehouse.exists(write_into):
                    lakehouse.create_empty(write_into, pa.Table.from_batches([enriched]).schema)
                created = True
                table_setup_s += time.perf_counter() - t0

            t0 = time.perf_counter()
            data = pa.Table.from_batches([enriched])
            if writer is None:
                writer = lakehouse.data_file_writer(write_into)
            files_written += 1
            pending.append(
                writer.write(data, name=f"loom-{run_id.hex}-{files_written:06d}.parquet")
            )
            parquet_write_s = time.perf_counter() - t0

            group_batches += 1
            group_rows += batch.num_rows
            group_cursor = batch_cursor
            if group_batches >= commit_every:
                register_s, progress_s = _register_and_report()

        batches.append(
            BatchRecord(
                index=len(batches),
                rows=batch.num_rows,
                raw_bytes=int(batch.nbytes),
                read_s=read_s,
                transform_s=transform_s,
                parquet_write_s=parquet_write_s,
                register_s=register_s,
                progress_s=progress_s,
            )
        )
        if args.log_every > 0 and len(batches) % args.log_every == 0:
            done = sum(b.raw_bytes for b in batches)
            elapsed = time.perf_counter() - loop_start
            print(
                f"    [{cell} lô {len(batches):>3}] dòng={batch.num_rows:,} "
                f"cum={done / 1e6:7.1f}MB tb={(done / 1e6) / elapsed:6.2f}MB/s "
                f"| đọc={read_s:5.2f} bđ={transform_s:5.2f} ghi={parquet_write_s:5.2f} "
                f"đk={register_s:5.2f} báo={progress_s:5.2f}",
                flush=True,
            )
        if args.max_batches is not None and len(batches) >= args.max_batches:
            print(f"    --max-batches={args.max_batches}: dừng sớm", flush=True)
            break

    # Nhóm CUỐI gần như luôn dở dang, và bỏ nó là mất đúng ngần ấy dòng mỗi lần
    # chạy, trong im lặng — `run_incremental` có một lời commit thứ hai sau vòng
    # lặp vì đúng lý do này.
    if pending:
        register_s, progress_s = _register_and_report()
        batches[-1].register_s += register_s
        batches[-1].progress_s += progress_s

    if args.mode == "full":
        swap = _measure_swap(
            lakehouse=lakehouse,
            catalog=catalog,
            args=args,
            staging=staging,
            target=target,
            old_target=old_target,
        )

    # Đồng hồ tường DỪNG ở đây: cú tráo là chi phí cố định, và gộp nó vào mẫu số
    # của chi phí/MB sẽ trộn hai câu hỏi khác nhau. Nó được báo riêng, đầy đủ.
    seconds = time.perf_counter() - loop_start

    return {
        "seconds": seconds,
        "rows": sum(b.rows for b in batches),
        "arrow_bytes": sum(b.raw_bytes for b in batches),
        "wire_bytes": 0,
        "stages_separable": not per_batch_commit,
        "table_setup_s": table_setup_s,
        "batches": [asdict(b) for b in batches],
        "swap": asdict(swap) if swap is not None else None,
    }


def _cursor_of(batch: pa.RecordBatch, column: str) -> int:
    """Mốc cursor của một lô. `max()` là chi phí CỦA việc báo watermark.

    `run_incremental` tính nó ngay trước lời gọi báo, nên trong production nó thuộc
    giai đoạn 5. Ở ĐÂY nó cố ý nằm NGOÀI cả năm đồng hồ giai đoạn: cả hai đường ghi
    trả nó y hệt nhau, nên nó không phân biệt được chúng, và một đại lượng không
    phân biệt được hai vế mà lại chỉ được bấm giờ ở MỘT vế là một sai lệch thuần.
    Nó vẫn nằm trong đồng hồ TƯỜNG của cả hai ô, nên nó không biến mất khỏi phép
    trừ — nó chỉ không bị gán cho một giai đoạn ở một vế mà thôi.
    """
    return int(max(batch.column(column).to_pylist()))


def _measure_swap(
    *,
    lakehouse: Lakehouse,
    catalog: RestCatalog,
    args: argparse.Namespace,
    staging: str,
    target: str,
    old_target: str,
) -> SwapRecord:
    t0 = time.perf_counter()
    staged = lakehouse.exists(staging)
    staging_done_s = time.perf_counter() - t0
    if not staged:
        raise SystemExit("không có bảng staging — nguồn đọc ra rỗng")

    t0 = time.perf_counter()
    had_target = lakehouse.exists(target)
    target_exists_s = time.perf_counter() - t0

    rename_away_s = 0.0
    drop_old_s = 0.0
    purge_s = 0.0
    purged = 0
    old_location: str | None = None
    if had_target:
        old_location = catalog.load_table(target).location()
        t0 = time.perf_counter()
        lakehouse.rename_table(target, old_target)
        rename_away_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    lakehouse.rename_table(staging, target)
    promote_s = time.perf_counter() - t0

    if had_target:
        t0 = time.perf_counter()
        lakehouse.drop_table(old_target)
        drop_old_s = time.perf_counter() - t0
        if old_location is not None:
            t0 = time.perf_counter()
            purged = purge_s3_prefix(
                s3_endpoint=args.minio_endpoint,
                access_key=os.environ["MINIO_ACCESS_KEY"],
                secret_key=os.environ["MINIO_SECRET_KEY"],
                bucket=args.bucket,
                prefix=_s3_key_of(old_location, args.bucket),
            )
            purge_s = time.perf_counter() - t0

    return SwapRecord(
        staging_done_s=staging_done_s,
        target_exists_s=target_exists_s,
        rename_target_away_s=rename_away_s,
        promote_staging_s=promote_s,
        drop_old_target_s=drop_old_s,
        purge_s3_s=purge_s,
        purged_objects=purged,
        had_target=had_target,
    )


# ─────────────────────────── fork mỗi mẫu ────────────────────────────


def measure_isolated(work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Chạy `work` trong một tiến trình CON và mang về `ru_maxrss` của chính nó.

    Con KHÔNG BAO GIỜ chạy tiếp mã của cha: `os._exit` bỏ qua mọi handler
    `atexit`, mọi khối `finally` đang chờ trong stack của cha, và — quan trọng
    nhất — khối dọn dẹp ở `main`. Một con thoát bằng `sys.exit` sẽ chạy khối dọn
    dẹp đó lần thứ hai, xoá warehouse ngay giữa lần chạy của cha.
    """
    at_fork = rss_mib()
    read_fd, write_fd = os.pipe()
    pid = os.fork()

    if pid == 0:
        payload: dict[str, Any]
        code = 0
        try:
            payload = work()
        except BaseException as exc:  # con phải BÁO được lỗi, không chết câm
            payload = {"error": f"{type(exc).__name__}: {exc}"}
            code = 1
        payload["peak_rss_mib"] = rss_mib()
        payload["rss_at_fork_mib"] = at_fork
        os.close(read_fd)
        sys.stdout.flush()
        with os.fdopen(write_fd, "w") as channel:
            json.dump(payload, channel)
        os._exit(code)

    os.close(write_fd)
    with os.fdopen(read_fd) as channel:
        raw = channel.read()
    os.waitpid(pid, 0)

    if not raw:
        # Con chết trước khi kịp ghi gì — OOMKill hoặc segfault. Nói ra điều đó
        # thay vì để `json.loads("")` ném `JSONDecodeError`, một câu không gợi ý gì
        # về việc vừa có một tiến trình bị giết.
        return {
            "error": "tiến trình con chết mà không gửi kết quả nào "
            "(nhiều khả năng bị giết: OOM hoặc tín hiệu)",
            "peak_rss_mib": 0.0,
            "rss_at_fork_mib": at_fork,
        }
    result: dict[str, Any] = json.loads(raw)
    return result


# ─────────────────────────── warehouse ────────────────────────────


@dataclass
class Bench:
    """Mọi thứ một lần đo cần, dựng một lần rồi truyền đi."""

    catalog_factory: Callable[[], RestCatalog]
    bucket: str
    key_prefix: str
    warehouse_id: str
    warehouse_name: str
    s3_endpoint: str
    access_key: str
    secret_key: str
    namespace: str


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

    def catalog_factory() -> RestCatalog:
        # MỘT catalog cho MỖI tiến trình con: `RestCatalog` giữ một session HTTP
        # với socket đang mở, và hai tiến trình cùng ghi lên một socket TCP thì
        # hỏng theo cách rất khó đọc.
        return build_catalog(
            catalog_uri=f"{management_url}/catalog",
            warehouse=warehouse_name,
            s3_endpoint=args.minio_endpoint,
        )

    return Bench(
        catalog_factory=catalog_factory,
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
    """Best-effort, và MỌI bước bọc riêng: dọn dẹp chạy SAU khi số đã chốt, nên một
    lỗi dọn không được phép đổi mã thoát của một phép đo đã tính đúng — đúng cái bẫy
    `probe_iceberg_single_commit.py` đã ăn một lần (Lakekeeper trả 403, không phải
    404, cho một bảng đã bị rename đi mất).

    Liệt kê THẬT qua `list_tables` chứ không theo danh sách tên đã tạo: cú tráo của
    `full` đổi tên bảng giữa chừng, nên danh sách tên đã tạo không còn khớp thực tế.
    """
    catalog = bench.catalog_factory()
    lakehouse = Lakehouse(catalog)
    try:
        for info in lakehouse.list_tables(bench.namespace):
            try:
                catalog.drop_table(info.qualified)
            except Exception as exc:  # bắt rộng có chủ đích — dọn dẹp không được ném
                print(f"  dọn: drop_table {info.qualified} lỗi {type(exc).__name__}", flush=True)
    except Exception as exc:  # bắt rộng có chủ đích
        print(f"  dọn: list_tables lỗi {type(exc).__name__}", flush=True)
    try:
        catalog.drop_namespace(bench.namespace)
    except (NoSuchNamespaceError, NoSuchTableError):
        pass
    except Exception as exc:  # bắt rộng có chủ đích
        print(f"  dọn: drop_namespace lỗi {type(exc).__name__}", flush=True)
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.delete(f"{management_url}/management/v1/warehouse/{bench.warehouse_id}")
        if resp.status_code not in (204, 404):
            print(f"  dọn: xoá warehouse trả {resp.status_code}", flush=True)
    except Exception as exc:  # bắt rộng có chủ đích
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
    except Exception as exc:  # bắt rộng có chủ đích
        print(f"  dọn: purge S3 lỗi {type(exc).__name__}", flush=True)


# ─────────────────────────── tổng kết ────────────────────────────


def _stats(values: list[float]) -> tuple[float, float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0, 0.0)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return (statistics.median(values), min(values), max(values), stdev)


def _origin(picked: list[Sample], replayed: set[tuple[str, int]]) -> str:
    """Ô này đo lần này, phát lại, hay pha TRỘN?

    Một ô TRỘN là ô nguy hiểm nhất: trung vị của nó gộp hai điều kiện máy khác
    nhau, nên nó trông như một con số mà thật ra là hai. Vì vậy nó được gọi tên
    riêng, không gộp vào "có phát lại".
    """
    old = sum(1 for s in picked if s.key in replayed)
    if old == 0:
        return "đo lần này"
    if old == len(picked):
        return "PHÁT LẠI"
    return f"TRỘN {len(picked) - old}/{old}"


def print_summary(
    samples: list[Sample],
    *,
    replayed: set[tuple[str, int]],
    fingerprint: dict[str, Any],
    source_bytes: int,
    budget_ms_per_mb: float | None,
    state_dir: Path | None,
    progress_probe: dict[str, float] | None,
) -> int:
    """In số, và NÓI RÕ mẫu nào đo lần này, mẫu nào phát lại từ đĩa.

    Nối lại được là đúng cho một phép đo dài — nhưng in mẫu PHÁT LẠI y hệt mẫu VỪA
    ĐO, không một dấu hiệu nào, là một lỗi ĐỌC đã xảy ra thật ở
    `probe_read_path_cost.py`: một lần chạy không đo gì cả vẫn in ra một bảng đầy
    đủ trông như vừa đo xong, và những con số cũ đó suýt được báo cáo như một mốc
    nền mới. Chỗ sửa nó là chỗ in ra.

    Trả về mã thoát: 0 nếu đủ số để kết luận, 1 nếu không.
    """
    ok = [s for s in samples if s.error is None]
    broken = [s for s in samples if s.error is not None]

    print("", flush=True)
    print("=== NGUỒN MẪU ===", flush=True)
    n_replayed = sum(1 for s in samples if s.key in replayed)
    n_fresh = len(samples) - n_replayed
    stamps = sorted(s.measured_at for s in samples if s.measured_at)
    when = f"{stamps[0]} .. {stamps[-1]}" if stamps else "không có dấu thời gian"
    if state_dir is None:
        print(
            "KHÔNG dùng cache (`--state-dir` không đặt), nên mọi số dưới đây đo trong "
            f"LẦN CHẠY NÀY, khoảng {when}.",
            flush=True,
        )
    elif n_fresh == 0 and n_replayed:
        print(
            "*** TOÀN BỘ SỐ DƯỚI ĐÂY LÀ PHÁT LẠI TỪ ĐĨA — LẦN CHẠY NÀY KHÔNG ĐO GÌ. ***", flush=True
        )
        print(
            f"*** Đo lúc: {when}. Muốn số mới: --fresh, hoặc một --state-dir khác. ***", flush=True
        )
    elif n_replayed:
        print(
            f"*** TRỘN: {n_fresh} mẫu đo lần này, {n_replayed} mẫu PHÁT LẠI từ đĩa. "
            "Cột 'nguồn mẫu' bên dưới nói từng ô thuộc loại nào. ***",
            flush=True,
        )
        print(f"*** Toàn bộ mẫu trong khoảng: {when}. ***", flush=True)
    else:
        print(f"Mọi mẫu đo trong LẦN CHẠY NÀY, khoảng {when}.", flush=True)

    print("", flush=True)
    print("=== CẤU HÌNH ===", flush=True)
    for key, value in fingerprint.items():
        print(f"  {key:<14} {value}", flush=True)
    print(f"  {'source_bytes':<14} {source_bytes / 1e6:.0f} MB trên đĩa nguồn", flush=True)

    for sample in broken:
        print(f"  HỎNG {sample.cell}/rep{sample.rep}: {sample.error}", flush=True)

    # Mẫu số của thước đo: byte Arrow THÔ (`RecordBatch.nbytes`) — cùng đại lượng
    # và cùng ranh giới mà ĐO 3 và 2c dùng. Đo bằng một đại lượng khác thì con số
    # không so được với hai phép đo kia.
    ingest_ok = [s for s in ok if s.arrow_bytes > 0]
    if not ingest_ok:
        print("", flush=True)
        print(
            "KHÔNG KẾT LUẬN ĐƯỢC — không ô nạp nào chạy xong, nên không có mẫu số MB.", flush=True
        )
        return 1
    arrow_bytes = statistics.median(s.arrow_bytes for s in ingest_ok)
    arrow_mb = arrow_bytes / 1e6
    rows = statistics.median(s.rows for s in ingest_ok)
    spread = {s.arrow_bytes for s in ingest_ok}
    print(
        f"  {'MB Arrow':<14} {arrow_mb:.1f} MB ({rows:,.0f} dòng x "
        f"{arrow_bytes / rows:.1f} byte/dòng)"
        + ("" if len(spread) == 1 else f"  — CẢNH BÁO: các ô nạp đọc khác nhau {sorted(spread)}"),
        flush=True,
    )

    print("", flush=True)
    print("=== TỪNG MẪU (đây là dữ liệu; trung vị bên dưới chỉ là bản tóm) ===", flush=True)
    for cell in ALL_CELLS:
        picked = sorted((s for s in ok if s.cell == cell), key=lambda s: s.rep)
        if not picked:
            continue
        for s in picked:
            mark = " [PHÁT LẠI]" if s.key in replayed else ""
            print(
                f"  {cell:<10} rep{s.rep} {s.seconds:8.3f}s  "
                f"RSS đỉnh {s.peak_rss_mib:6.1f} MiB (nền {s.rss_at_fork_mib:.1f})  "
                f"{s.measured_at}{mark}",
                flush=True,
            )

    print("", flush=True)
    print("=== GIAI ĐOẠN (tổng qua các lô, trung vị các mẫu của ô) ===", flush=True)
    header = (
        f"{'ô':<10} {'n':>2} {'1.đọc':>8} {'2.bđổi':>8} {'3.parquet':>10} "
        f"{'4.đăng ký':>10} {'5.báo':>8} {'tường':>9} {'nguồn mẫu':>12}"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for cell in (CELL_ADD_FILES, CELL_APPEND):
        picked = [s for s in ok if s.cell == cell]
        if not picked:
            continue
        totals = [s.stage_totals() for s in picked]
        med = {k: statistics.median(t[k] for t in totals) for k in totals[0]}
        note = "" if picked[0].stages_separable else "  (3+4 KHÔNG tách được -> cả cục ở 4)"
        print(
            f"{cell:<10} {len(picked):>2} {med['read_s']:>8.2f} {med['transform_s']:>8.2f} "
            f"{med['parquet_write_s']:>10.2f} {med['register_s']:>10.2f} "
            f"{med['progress_s']:>8.2f} {statistics.median(s.seconds for s in picked):>9.2f} "
            f"{_origin(picked, replayed):>12}{note}",
            flush=True,
        )
        setup = statistics.median(s.table_setup_s for s in picked)
        commits = statistics.median(sum(1 for b in s.batches if b.register_s > 0) for s in picked)
        print(
            f"           dựng bảng {setup:.2f}s (ngoài năm giai đoạn, TRONG đồng hồ tường), "
            f"{statistics.median(len(s.batches) for s in picked):.0f} lô / {commits:.0f} commit",
            flush=True,
        )
    if any(s.cell == CELL_COPY for s in ok):
        wire = statistics.median(s.wire_bytes for s in ok if s.cell == CELL_COPY)
        print(
            f"{CELL_COPY:<10} {sum(1 for s in ok if s.cell == CELL_COPY):>2} "
            f"{'—':>8} {'—':>8} {'—':>10} {'—':>10} {'—':>8} "
            f"{statistics.median(s.seconds for s in ok if s.cell == CELL_COPY):>9.2f} "
            f"{_origin([s for s in ok if s.cell == CELL_COPY], replayed):>12}"
            f"   ({wire / 1e6:.0f} MB trên dây, định dạng text)",
            flush=True,
        )

    if progress_probe is not None:
        print("", flush=True)
        print(
            f"Chi phí báo watermark (cặp so le, n={int(progress_probe['pairs'])}): "
            f"rows-một-mình={progress_probe['rows_only_median_ms']:.1f}ms  "
            f"rows+cursor={progress_probe['with_cursor_median_ms']:.1f}ms  "
            f"CHÊNH={progress_probe['watermark_extra_ms']:.1f}ms/nhóm",
            flush=True,
        )

    for cell in (CELL_ADD_FILES, CELL_APPEND):
        swaps = [s.swap for s in ok if s.cell == cell and s.swap is not None]
        for swap in swaps:
            catalog_swap_s = (
                swap.staging_done_s
                + swap.target_exists_s
                + swap.rename_target_away_s
                + swap.promote_staging_s
                + swap.drop_old_target_s
            )
            print(
                f"Cú tráo {cell} (CHI PHÍ CỐ ĐỊNH của full): {catalog_swap_s:.3f}s "
                f"[staging_done={swap.staging_done_s:.3f} "
                f"target_exists={swap.target_exists_s:.3f} "
                f"rename_away={swap.rename_target_away_s:.3f} "
                f"promote={swap.promote_staging_s:.3f} drop_old={swap.drop_old_target_s:.3f}] "
                f"had_target={swap.had_target}; purge S3 {swap.purge_s3_s:.3f}s cho "
                f"{swap.purged_objects} object (đường nạp thật KHÔNG chạy bước purge)",
                flush=True,
            )

    return _print_client_cost(
        ok,
        attempted={s.cell for s in samples},
        replayed=replayed,
        arrow_mb=arrow_mb,
        budget_ms_per_mb=budget_ms_per_mb,
    )


def _print_client_cost(
    ok: list[Sample],
    *,
    attempted: set[str],
    replayed: set[tuple[str, int]],
    arrow_mb: float,
    budget_ms_per_mb: float | None,
) -> int:
    copy_samples = [s.seconds for s in ok if s.cell == CELL_COPY]
    print("", flush=True)
    print("=== CHI PHÍ CLIENT / MB  (spec 3d mục 2) ===", flush=True)
    if not copy_samples:
        print(
            "KHÔNG TÍNH ĐƯỢC — thiếu ô `copy`. Hiệu số không tồn tại (không phải bằng 0), "
            "nên không có con số nào ở đây là hợp lệ.",
            flush=True,
        )
        return 1

    copy_median, copy_lo, copy_hi, copy_sd = _stats(copy_samples)
    print(
        f"`COPY ... TO STDOUT` thô: trung vị {copy_median:.3f}s  "
        f"min-max {copy_lo:.3f}-{copy_hi:.3f}  sd {copy_sd:.3f}  n={len(copy_samples)}",
        flush=True,
    )
    # Hai vế của phép trừ PHẢI đọc cùng ngần ấy dòng. Nếu không, hiệu số là hiệu
    # của hai lượng dữ liệu khác nhau — một con số vô nghĩa trông hoàn toàn hợp lệ.
    copy_rows = {s.rows for s in ok if s.cell == CELL_COPY}
    ingest_rows = {s.rows for s in ok if s.arrow_bytes > 0}
    if copy_rows and ingest_rows and copy_rows != ingest_rows:
        print(
            f"*** CẢNH BÁO: ô `copy` rút {sorted(copy_rows)} dòng, ô nạp đọc "
            f"{sorted(ingest_rows)} dòng. Hiệu số dưới đây KHÔNG so hai vế cùng dữ "
            "liệu — đọc nó như một dấu hiệu hỏng, không như một số đo. ***",
            flush=True,
        )
    print(
        f"Mẫu số: {arrow_mb:.1f} MB byte Arrow thô (KHÔNG phải byte trên dây) — cùng đại "
        "lượng ĐO 3 và 2c dùng.",
        flush=True,
    )
    print("", flush=True)
    header = (
        f"{'ô':<10} {'n':>2} {'tường tv':>9} {'hiệu s':>8} {'ms/MB':>9} "
        f"{'MB/s':>7} {'nguồn mẫu':>12}"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)
    costs: dict[str, float] = {}
    for cell in (CELL_ADD_FILES, CELL_APPEND):
        picked = [s for s in ok if s.cell == cell]
        if not picked:
            continue
        median = statistics.median(s.seconds for s in picked)
        delta = median - copy_median
        ms_per_mb = delta * 1000 / arrow_mb
        costs[cell] = ms_per_mb
        print(
            f"{cell:<10} {len(picked):>2} {median:>9.3f} {delta:>8.3f} {ms_per_mb:>9.1f} "
            f"{arrow_mb / median:>7.2f} {_origin(picked, replayed):>12}",
            flush=True,
        )
    print("", flush=True)
    print(
        "MB/s là số người ta muốn biết, nhưng nó KHÔNG phải cổng chặn: nó chủ yếu nói về "
        "đường truyền tới nguồn. ms/MB là cổng chặn, vì phần đường truyền có mặt ở cả hai "
        "vế của phép trừ nên nó triệt tiêu.",
        flush=True,
    )

    if budget_ms_per_mb is None:
        print("", flush=True)
        print(
            "N (ngân sách ms/MB) CHƯA CHỐT, và script này KHÔNG bịa một cái. Ngưỡng cũ sai "
            "hai lần vì được chốt TRƯỚC khi đo. Cách chốt (spec 3d mục 2): đặt N ở giữa hai "
            "con số trên — một mức đường đã sửa ĐẠT và đường cũ TRƯỢT — rồi ghi cả hai số "
            "và lý do chọn vào báo cáo, và chạy lại với `--budget-ms-per-mb <N>` để có một "
            "cổng chặn kiểm được.",
            flush=True,
        )
        return 0

    new = costs.get(CELL_ADD_FILES)
    if new is None:
        # Phân biệt "ô KHÔNG được yêu cầu" với "ô HỎNG". Cả hai đều làm N không áp
        # được vào đâu, nhưng chúng là hai sự cố khác nhau và người đọc phải sửa hai
        # thứ khác nhau — một câu "không chạy xong" cho cả hai trường hợp gửi người
        # đi tìm một lỗi không tồn tại.
        if CELL_ADD_FILES not in attempted:
            print(
                f"N ĐÃ ĐẶT nhưng lần chạy này KHÔNG có ô `{CELL_ADD_FILES}` (xem "
                "`--cells`), nên không có gì để so với N. Đây là một lần chạy đối "
                "chứng, không phải một lần chạy qua cổng chặn.",
                flush=True,
            )
        else:
            print(f"KHÔNG KẾT LUẬN ĐƯỢC — ô `{CELL_ADD_FILES}` HỎNG, xem lỗi ở trên.", flush=True)
        return 1
    verdict = "ĐẠT" if new <= budget_ms_per_mb else "KHÔNG ĐẠT"
    print("", flush=True)
    print(
        f"N = {budget_ms_per_mb:.1f} ms/MB   đường hôm nay = {new:.1f} ms/MB   {verdict}",
        flush=True,
    )
    old = costs.get(CELL_APPEND)
    if old is not None:
        holds = old > budget_ms_per_mb
        # Ô `append` là đường ghi CŨ ở CỠ LÔ HIỆN TẠI — nó KHÔNG phải cấu hình
        # trước 3d. Cấu hình đó là `--batch-rows 40000 --commit-every 1`, và nói
        # lẫn hai thứ là cách dễ nhất để báo phần cải thiện NHỎ HƠN thật: ở cỡ lô
        # mới, đường cũ đã hưởng sẵn một nửa phần cải thiện (ít lô hơn -> ít
        # commit hơn), nên hiệu số ở đây chỉ đo phần `add_files` mua được.
        print(
            f"Đối chứng: đường ghi CŨ ở CÙNG cỡ lô = {old:.1f} ms/MB, "
            f"{'TRƯỢT' if holds else 'CŨNG ĐẠT'} N.",
            flush=True,
        )
        if not holds:
            print(
                "Đó KHÔNG tự động nghĩa là N vô nghĩa: ô này chỉ đổi ĐƯỜNG GHI, còn cấu "
                "hình TRƯỚC 3d cũng có cỡ lô khác. Muốn con số 'trước' thật, chạy riêng "
                "`--batch-rows 40000 --commit-every 1 --cells copy,append` và so N với "
                "con số ĐÓ. Một N mà cấu hình trước-3d cũng đạt thì mới là một N không "
                "phân biệt được gì.",
                flush=True,
            )
    return 0 if verdict == "ĐẠT" else 1


# ─────────────────────────── CLI ────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source-kind",
        choices=("bench", "aiven"),
        default="bench",
        help="`bench` = Postgres dùng-một-lần TRONG CỤM (make bench-source-up). `aiven` = "
        "service của chủ dự án, CHỈ-ĐỌC qua _aiven_guard, và nó ĐÒI bảng có sẵn",
    )
    parser.add_argument("--source-schema", default="bench")
    parser.add_argument("--source-table", default="ingest_bench")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument(
        "--cells",
        default=",".join(ALL_CELLS),
        help="Ô cần đo, phân cách bằng dấu phẩy. `copy` là mẫu số của phép trừ — bỏ nó thì "
        "không có chi phí client nào tính được",
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=3,
        help="Số lần đo MỖI ô. >=3 vì một tải đồng thời đã từng làm phồng mẫu ~3x và "
        "trung vị của hai mẫu không cho thấy điều đó",
    )
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=ReadTuning().batch_rows,
        help="Mặc định = mặc định THẬT của production (`ReadTuning`)",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=WriteTuning().commit_every_batches,
        help="K của đường add_files. Mặc định = mặc định THẬT (`WriteTuning`)",
    )
    parser.add_argument(
        "--budget-ms-per-mb",
        type=float,
        default=None,
        help="N. KHÔNG có mặc định, có chủ đích — xem docstring đầu file",
    )
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
    parser.add_argument("--progress-probe", type=int, default=0)
    parser.add_argument(
        "--cursor-offset",
        type=int,
        default=0,
        help="Cộng vào giá trị watermark GỬI ĐI (không đụng dữ liệu đọc) — xem ProgressReporter",
    )
    parser.add_argument("--pre-create-target", action="store_true", default=True)
    parser.add_argument("--no-pre-create-target", dest="pre_create_target", action="store_false")
    parser.add_argument("--max-batches", type=int, default=None, help="Chạy thử nhanh")
    parser.add_argument("--log-every", type=int, default=0)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Bật nối lại: mẫu ghi vào <dir>/progress.json ngay khi xong. Thiếu nó thì KHÔNG "
        "có cache nào — đúng cấu hình cho một Job không có volume bền",
    )
    parser.add_argument("--fresh", action="store_true", help="Bỏ progress.json cũ, đo lại từ đầu")
    parser.add_argument(
        "--report-only", action="store_true", help="Chỉ in tổng kết từ progress.json đã lưu"
    )
    parser.add_argument("--keep", action="store_true", help="KHÔNG dọn — chỉ để gỡ lỗi")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Source(kind=args.source_kind, schema=args.source_schema, table=args.source_table)

    state_path = args.state_dir / "progress.json" if args.state_dir is not None else None
    if state_path is not None:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        if args.fresh and state_path.exists():
            state_path.unlink()
            print(f"--fresh: đã xoá {state_path}", flush=True)

    if args.report_only:
        if state_path is None or not state_path.exists():
            print("Không có progress.json — chưa có lần chạy nào để in.", flush=True)
            return 1
        saved = load_progress(state_path)
        return print_summary(
            saved.samples,
            replayed={s.key for s in saved.samples},
            fingerprint=saved.fingerprint,
            source_bytes=0,
            budget_ms_per_mb=args.budget_ms_per_mb,
            state_dir=args.state_dir,
            progress_probe=None,
        )

    # Đòi bảng nguồn TRƯỚC khi dựng warehouse: dựng warehouse rồi mới phát hiện
    # thiếu nguồn để lại rác trên MinIO/Lakekeeper.
    source_rows, source_bytes = require_source_table(source)
    print(
        f"bảng nguồn [{source.kind}] {source.schema}.{source.table}: "
        f"{source_rows:,} dòng, {source_bytes / 1e6:.0f} MB (CHỈ ĐỌC)",
        flush=True,
    )

    fingerprint = _fingerprint(args, source_rows)
    progress = Progress(fingerprint=fingerprint)
    replayed: set[tuple[str, int]] = set()
    if state_path is not None and state_path.exists():
        saved = load_progress(state_path)
        if saved.fingerprint != fingerprint:
            raise SystemExit(
                f"TỪ CHỐI: progress.json đo {saved.fingerprint}, lần này {fingerprint}. "
                "Trộn hai cấu hình vào một bảng tổng kết là cách chắc chắn nhất để báo một "
                "con số của một hệ thống không tồn tại. Dùng --fresh hoặc một --state-dir khác."
            )
        progress = saved
        replayed = {s.key for s in saved.samples}
        print(f"Tiếp tục từ {state_path} ({len(saved.samples)} mẫu đã có).", flush=True)

    # Credential GỐC MinIO: Lakekeeper cần chúng để tự AssumeRole hộ lúc tạo
    # warehouse (xem `create_warehouse`). Client PyIceberg không bao giờ thấy cặp
    # này. `KeyError` với traceback rõ hơn một 403 mù mờ nếu quên mount.
    access_key = os.environ["MINIO_ACCESS_KEY"]
    secret_key = os.environ["MINIO_SECRET_KEY"]

    wanted = [name.strip() for name in args.cells.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in ALL_CELLS]
    if unknown:
        raise SystemExit(f"ô không biết: {unknown}. Chọn trong {list(ALL_CELLS)}")

    reporter: ProgressReporter | None = None
    if args.run_id is not None:
        secret = os.environ.get("LOOM_INGEST_SHARED_SECRET")
        if not secret:
            raise SystemExit("có --run-id nhưng thiếu LOOM_INGEST_SHARED_SECRET")
        reporter = ProgressReporter(args.api_base_url, args.run_id, secret, args.cursor_offset)

    # Cột nào và thứ tự nào — đọc TỪ nguồn, để ô `copy` không thể lệch khỏi ô nạp.
    connector = PostgresConnector(
        connector_dsn(source), batch_rows=args.batch_rows, schema=source.schema
    )
    stream = f"{source.schema}.{source.table}"
    streams = connector.discover()
    cursor = resolve_cursor(streams, stream, None)
    columns = [
        column.name for schema in streams if schema.name == stream for column in schema.columns
    ]
    print(
        f"cursor: {cursor.name} ({cursor.cursor_type})   {len(columns)} cột: {columns}", flush=True
    )

    bench = _fresh_warehouse(args, access_key, secret_key)
    probe: dict[str, float] | None = None
    try:
        # XEN KẼ: rep 1 của cả ba ô, rồi rep 2... Một tải đồng thời rơi vào CẢ BA ô
        # thay vì chỉ ô đang chạy — xem docstring đầu file.
        for rep in range(1, args.reps + 1):
            for cell in wanted:
                if (cell, rep) in replayed:
                    print(f"[{cell} rep{rep}] PHÁT LẠI từ đĩa — không đo lại.", flush=True)
                    continue
                print(f"[{cell} rep{rep}] đo...", flush=True)
                target = f"{args.iceberg_namespace}.{args.target_table}_{cell}_{rep}"
                measured_at = _now_iso()
                raw = measure_isolated(
                    _work_for(
                        cell=cell,
                        args=args,
                        source=source,
                        bench=bench,
                        target=target,
                        columns=columns,
                        order_by=cursor.name,
                        reporter=reporter,
                    )
                )
                sample = _sample_from_payload(cell=cell, rep=rep, measured_at=measured_at, raw=raw)
                progress.samples.append(sample)
                if state_path is not None:
                    save_progress(state_path, progress)
                if sample.error is not None:
                    print(f"    HỎNG: {sample.error}", flush=True)
                else:
                    print(
                        f"    {sample.seconds:.3f}s  RSS đỉnh {sample.peak_rss_mib:.1f} MiB  "
                        f"{sample.rows:,} dòng",
                        flush=True,
                    )

        if reporter is not None and args.progress_probe > 0:
            # Chạy SAU vòng lặp: nó đẩy watermark của run lên một giá trị tổng hợp,
            # và làm thế TRƯỚC sẽ khiến mọi lô sau đó rơi vào nhánh "không tiến" và
            # đo nhầm nhánh rẻ.
            probe = probe_progress_cost(
                reporter, pairs=args.progress_probe, cursor_base=source_rows * 10
            )

        code = print_summary(
            progress.samples,
            replayed=replayed,
            fingerprint=fingerprint,
            source_bytes=source_bytes,
            budget_ms_per_mb=args.budget_ms_per_mb,
            state_dir=args.state_dir,
            progress_probe=probe,
        )
        print("RESULT_JSON=" + json.dumps([asdict(s) for s in progress.samples]), flush=True)
        return code
    finally:
        if reporter is not None:
            reporter.close()
        if args.keep:
            print(f"--keep: GIỮ warehouse {bench.warehouse_name} — dọn tay", flush=True)
        else:
            _teardown(bench, args.lakekeeper_url)


def _work_for(
    *,
    cell: str,
    args: argparse.Namespace,
    source: Source,
    bench: Bench,
    target: str,
    columns: list[str],
    order_by: str,
    reporter: ProgressReporter | None,
) -> Callable[[], dict[str, Any]]:
    """Đóng gói một ô thành một `work()` cho `measure_isolated`."""
    if cell == CELL_COPY:
        return lambda: copy_to_stdout_cost(source, columns, order_by)
    return lambda: ingest_cost(
        cell=cell,
        args=args,
        source=source,
        catalog=bench.catalog_factory(),
        target=target,
        reporter=reporter,
    )


def _sample_from_payload(*, cell: str, rep: int, measured_at: str, raw: dict[str, Any]) -> Sample:
    if "error" in raw:
        return Sample(
            cell=cell,
            rep=rep,
            measured_at=measured_at,
            seconds=0.0,
            rows=0,
            arrow_bytes=0,
            wire_bytes=0,
            stages_separable=False,
            table_setup_s=0.0,
            peak_rss_mib=float(raw.get("peak_rss_mib", 0.0)),
            rss_at_fork_mib=float(raw.get("rss_at_fork_mib", 0.0)),
            error=str(raw["error"]),
        )
    swap = raw.get("swap")
    return Sample(
        cell=cell,
        rep=rep,
        measured_at=measured_at,
        seconds=float(raw["seconds"]),
        rows=int(raw["rows"]),
        arrow_bytes=int(raw["arrow_bytes"]),
        wire_bytes=int(raw["wire_bytes"]),
        stages_separable=bool(raw["stages_separable"]),
        table_setup_s=float(raw["table_setup_s"]),
        peak_rss_mib=float(raw["peak_rss_mib"]),
        rss_at_fork_mib=float(raw["rss_at_fork_mib"]),
        batches=[BatchRecord(**b) for b in raw.get("batches") or []],
        swap=SwapRecord(**swap) if swap else None,
    )


if __name__ == "__main__":
    sys.exit(main())
