"""Thăm dò `Table.add_files()` của Giai đoạn 3a — N file Parquet có vào được MỘT
snapshot duy nhất không, và nếu có thì nó tốn gì.

## Vì sao phép thăm dò này tồn tại

ĐO 3 (`docs/measurements/2026-08-13-phase-3a-ingest-path.md`) đo đường nạp ở
**1,5 MB/s** trên ngưỡng 14,7 MB/s, và tách được chỗ mất thời gian: với
`batch_rows=10.000`, **commit catalog chiếm 44,0%** (42,1s trên 97,0s) cộng báo
tiến độ 21,4% — hai phần ba thời gian là chi phí CỐ ĐỊNH MỖI LÔ trải trên gần
như không có byte nào. Sàn đo được là ~0,83 giây mỗi commit BẤT KỂ lô to cỡ nào;
Giai đoạn 2c ghi 250 MB mỗi commit, đường nạp ghi 2,98 MB mỗi commit — nhiều hơn
**84 lần** số commit trên cùng một lượng byte.

Lối thoát thông thường là "gom nhiều, commit ít". ĐO 2
(`scripts/probe_iceberg_single_commit.py`) đã ĐÓNG lối đó cho một API cụ thể:
`table.transaction()` của PyIceberg 0.11.1 KHÔNG gộp — hai `tx.append` cho ra 2
snapshot, và giữ transaction mở còn tốn RAM HƠN (499 so với 406 MiB) chứ không
ít hơn. Đó là lý do `full` hôm nay commit từng lô.

**Nhưng ĐO 2 chỉ thử `transaction()` + `append`. Nó chưa bao giờ thử
`Table.add_files()`** — API đăng ký những file Parquet ĐÃ GHI XONG vào bảng. Nếu
nó hạ N file vào MỘT snapshot thì nó gỡ được đúng cái nút mà ĐO 2 tưởng đã thắt
chặt: ghi Parquet theo luồng (RAM chặn theo MỘT lô, y như hôm nay), rồi commit
MỘT lần cho cả lần nạp — cắt ~50 lần commit xuống 1 mà không nâng RAM.

Đọc mã `pyiceberg/table/__init__.py` bản 0.11.1 cho thấy `Transaction.add_files`
mở ĐÚNG MỘT `_append_snapshot_producer` rồi `append_data_file` cho từng file
trong vòng lặp — hình dạng đó gợi ý 1 snapshot bất kể N. **Đọc mã KHÔNG phải
bằng chứng**: đúng loại suy luận đó đã làm giả định của ĐO 2 sai (API
`transaction` cũng "trông transactional"). Nên script này gọi thật lên Lakekeeper
thật rồi đếm `len(table.snapshots())`, con số duy nhất không nói dối.

## Năm câu hỏi, và câu nào quyết định mã thoát

  Q1. `add_files()` với N file có sinh ĐÚNG 1 snapshot mới không? N = 1, 5, 20.
      **ĐÂY LÀ CỬA CHẶN** — nếu N file cho N snapshot thì `add_files` chỉ là một
      cách viết khác của vòng lặp `append` hôm nay và cả hướng đi chết tại chỗ.
      Đếm snapshot TRƯỚC/SAU, và đọc lại đủ số dòng: một snapshot không đăng ký
      được dòng nào cũng là "1 snapshot" mà vô dụng.

  Q2. Đỉnh RSS so với đường `append` từng lô, cùng dữ liệu. Theo cách của ĐO 1:
      `resource.getrusage(...).ru_maxrss`, KHÔNG phải `memory.current` của
      cgroup — một máy chủ lưu trữ lấp đầy page cache tới trần chứng minh được
      con số của chính nó chứ không phải của tiến trình đang đo.

  Q3. Thời gian tường so với 50 lần commit `append`.

  Q4. RÀNG BUỘC — đo, không đoán:
      a. Parquet do pyarrow thuần ghi KHÔNG mang field ID của Iceberg.
         `add_files` có cần name-mapping không, và PyIceberg 0.11.1 có tự cấp
         một cái không?
      b. File phải nằm ở ĐÂU — trong location của chính bảng, hay bất cứ đâu
         catalog đọc được?
      c. `check_duplicate_files` tốn gì, và tắt đi có an toàn không?
      d. Schema lệch thì ỒN hay IM?

  Q5. Nó có ghép được với chuỗi tráo bảng ba bước của `full` không (xem
      `loom_task.sink`: `rename_target_away` -> `promote_staging` ->
      `drop_old_target`)?

Chỉ **Q1** quyết định mã thoát. Q2-Q5 định hình THIẾT KẾ chứ không định hình
việc hướng này còn sống hay không — một `add_files` gộp được snapshot nhưng đắt
RAM vẫn là một phát hiện dùng được (đổi cỡ lô), còn một `add_files` không gộp
được snapshot thì không còn gì để thiết kế.

## Vì sao Q2 phải fork, chứ không đo hai đường trong một tiến trình

`ru_maxrss` là đỉnh CỘNG DỒN từ lúc tiến trình khởi động và KHÔNG BAO GIỜ giảm.
Đo hai đường nối tiếp nhau trong một tiến trình thì đường chạy SAU thừa hưởng
đỉnh của đường chạy TRƯỚC, nên nó không bao giờ đo được là thấp hơn — tức là
đúng kết quả mà Q2 tồn tại để tìm sẽ không bao giờ hiện ra. ĐO 2 sống chung với
điều này được vì A/B của nó dùng lô nhỏ tới mức đóng góp không đáng kể; ở đây
HAI đường xử lý CÙNG một khối dữ liệu, nên không có bên nào nhỏ để bỏ qua.

Mỗi đường vì vậy chạy trong một tiến trình con `fork()` riêng, tự đọc
`ru_maxrss` của CHÍNH NÓ rồi gửi về qua pipe. Một cảnh báo phải nói ra: RSS của
con KHỞI ĐẦU xấp xỉ RSS của cha tại thời điểm fork (không gian địa chỉ là bản
sao copy-on-write), nên con số tuyệt đối của con ĐÃ BAO GỒM nền của cha. Vì vậy
(1) hai con được fork LIÊN TIẾP ở đầu lần chạy, trước khi cha kịp làm gì nặng,
để hai nền bằng nhau, và (2) script in RA nền đó (`rss_at_fork_mib`) cạnh đỉnh,
để người đọc trừ được chứ không phải tin.

Tiến trình con dựng `RestCatalog` MỚI của riêng nó (không dùng lại đối tượng
thừa hưởng từ cha): `RestCatalog` giữ một `requests.Session` với socket đang mở,
và hai tiến trình cùng ghi lên một socket TCP thì hỏng theo cách rất khó đọc.
Cha KHÔNG chạm catalog của mình trong lúc con sống.

Python 3.12 in một `DeprecationWarning` ở `os.fork()` vì tiến trình này đa
luồng (thư viện S3/HTTP dựng luồng nền). Cảnh báo đó ĐÚNG và không bị tắt đi:
rủi ro thật là con kế thừa một lock đang bị một luồng của cha giữ, rồi treo.
Chấp nhận có điều kiện, vì con KHÔNG dùng lại đối tượng nào của cha — nó dựng
catalog, filesystem và mọi client của riêng nó sau khi fork — nên vùng nguy hiểm
chỉ còn là các lock nội bộ của allocator/logging. Cái giá của việc sai: một Job
treo tới hết 900s rồi bị `make` cắt, KHÔNG phải một số đo sai. Đó là loại hỏng
nhìn thấy được, nên nó là loại hỏng chấp nhận được ở một phép thăm dò.

## Vì sao Parquet của đường add_files ghi qua `table.io`, không qua boto3

`table.io.new_output(...)` dùng đúng credential mà Lakekeeper VEND cho client
(STS hẹp theo key-prefix, xem `loom_iceberg.catalog`) — tức là đúng thứ pod nạp
thật có trong tay. Pod nạp KHÔNG có credential gốc của MinIO và không nên có.
Ghi bằng boto3 với credential gốc sẽ đo một đường mà production không đi được.

Phần MÃ HOÁ Parquet vẫn là `pyarrow.parquet.write_table` thuần — `table.io` chỉ
cấp cái ống byte. Nên tiền đề của Q4a ("Parquet do pyarrow thuần ghi không mang
field ID") KHÔNG bị đường ghi này làm sai lệch, và Q4a đo lại chính điều đó chứ
không giả định.

Riêng Q4b (file nằm ở đâu) CÓ dùng credential gốc qua `pyarrow.fs.S3FileSystem`,
có chủ đích: nó phải tách "catalog có ĐĂNG KÝ được file ở chỗ đó không" khỏi
"client có GHI được vào chỗ đó không". Trộn hai câu hỏi lại thì một lỗi 403 lúc
ghi sẽ bị đọc nhầm thành "add_files từ chối vị trí đó". Q4b đo CẢ HAI, riêng rẽ.

## Dọn dẹp

Bảng + namespace + warehouse + object S3, tất cả trong `finally`, best-effort.
Ba điều đã trả giá ở các phép đo trước và lặp lại nguyên vẹn ở đây:

  1. `drop_table`/`drop_namespace`/xoá warehouse KHÔNG xoá object trên S3 (đo ở
     Giai đoạn 2c) — phải `purge_s3_prefix` xoá thẳng qua S3 API.
  2. Dọn theo danh sách tên ĐANG CÓ (`catalog.list_tables`), KHÔNG theo tên đã
     TẠO: Q5 đổi tên bảng đi, và Lakekeeper trả **403 Forbidden, không phải
     404**, cho một tên không còn tồn tại — management API của nó cố ý không
     phân biệt "không tìm thấy" với "không được phép" cho một principal ẩn danh.
     PyIceberg vì thế ném `ForbiddenError` chứ không `NoSuchTableError`.
  3. Mọi bước dọn bọc try/except RIÊNG và KHÔNG BAO GIỜ ném tiếp: verdict đã
     chốt trước khi `finally` chạy, và một lỗi dọn dẹp làm hỏng mã thoát của một
     phép đo đã tính đúng là một false negative — tệ hơn cả không đo.

Q4b còn ghi file RA NGOÀI prefix của warehouse (đó chính là câu hỏi), nên prefix
đó được theo dõi riêng và purge riêng — không nằm dưới prefix warehouse nên
không có bước dọn nào khác chạm tới nó.

Chạy: `make probe-add-files` (Job trong cụm, `backoffLimit: 0` — thiếu cờ này
Kubernetes tự thử lại và để lại nhiều warehouse rác, đúng bẫy ĐO 1 đã ăn).
"""

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from resource import RUSAGE_SELF, getrusage
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pyarrow.fs import S3FileSystem  # type: ignore[import-untyped]
from pyiceberg.catalog.rest import RestCatalog

from loom_iceberg import build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse

if TYPE_CHECKING:
    # Xem chú thích TYPE_CHECKING trong measure_ingest_pod.py: image loom-query
    # build bằng `uv sync --frozen --no-dev` nên KHÔNG có `mypy_boto3_s3` lúc
    # chạy trong pod, dù có sẵn trên host qua `uv run`.
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

# Khoá metadata mà pyarrow gắn lên một field khi file Parquet CÓ field ID —
# chính là thứ Iceberg dùng để nối cột trong file với cột trong schema bảng.
# Vắng khoá này nghĩa là file không tự mô tả được mình theo ngôn ngữ Iceberg, và
# đó là lý do name-mapping tồn tại (Q4a).
_PARQUET_FIELD_ID = b"PARQUET:field_id"

# Thuộc tính bảng mà Iceberg lưu name-mapping vào (`TableProperties.
# DEFAULT_NAME_MAPPING` của PyIceberg). Viết ra hằng số thay vì import: cái tên
# này là một phần của SPEC Iceberg, không phải chi tiết cài đặt của PyIceberg,
# nên nó không được đổi cùng một lần nâng thư viện.
_NAME_MAPPING_PROPERTY = "schema.name-mapping.default"


def make_batch(rows: int, batch_index: int) -> pa.Table:
    """~268 byte/dòng, KHÁC NHAU từng dòng — bản sao nguyên xi từ
    `measure_ingest_pod.py` và `probe_iceberg_single_commit.py`.

    32 ký tự hex x 8 lần lặp = 256 byte văn bản + 8 byte int64 của `id` + ~4
    byte overhead offset chuỗi của Arrow ≈ 268 byte/dòng. Chuỗi lặp lại bị nén
    từ điển về gần 0 và biến bài đo thành bài đo của một bài toán khác — Giai
    đoạn 2a đã dính đúng bẫy đó một lần với `repeat('x', 512)`.
    """
    base = batch_index * rows
    return pa.table(
        {
            "id": pa.array([base + i for i in range(rows)], type=pa.int64()),
            "pad": pa.array(
                [uuid.uuid5(uuid.NAMESPACE_OID, str(base + i)).hex * 8 for i in range(rows)],
                type=pa.string(),
            ),
        }
    )


def snapshot_count(catalog: RestCatalog, identifier: str) -> int:
    """Tải LẠI bảng từ catalog rồi đếm snapshot.

    Phải tải lại, không dùng một `Table` đã giữ từ trước: commit cập nhật
    metadata trên đối tượng `Table` tại chỗ, nhưng con số đáng tin là con số
    catalog THẬT SỰ đã lưu, không phải bộ nhớ cục bộ của client.
    """
    return len(catalog.load_table(identifier).snapshots())


def row_count(catalog: RestCatalog, identifier: str) -> int:
    """Đếm dòng đọc lại được, theo LUỒNG.

    `to_arrow_batch_reader()` chứ không `to_arrow()`: bảng của Q2/Q3 là nửa
    triệu dòng, và nạp hết vào RAM chỉ để đếm sẽ làm chính tiến trình cha phình
    ra ngay trước khi nó fork tiến trình con tiếp theo — tức là làm hỏng nền RSS
    mà Q2 dựa vào (xem docstring đầu file).
    """
    total = 0
    for batch in catalog.load_table(identifier).scan().to_arrow_batch_reader():
        total += batch.num_rows
    return total


def rss_mib() -> float:
    """Đỉnh RSS của CHÍNH tiến trình này, MiB. Xem lý do chọn `ru_maxrss` thay vì
    `memory.current` của cgroup ở docstring `measure_ingest_pod.py`."""
    return float(getrusage(RUSAGE_SELF).ru_maxrss) / 1024


def one_line(text: str, limit: int = 170) -> str:
    """Ép một thông báo nhiều dòng thành MỘT dòng trước khi cắt.

    Không phải chuyện thẩm mỹ: khi schema lệch, PyIceberg 0.11.1 dựng một BẢNG
    `rich` nhiều dòng làm thông báo của `ValueError`. Cắt thẳng chuỗi đó cho ra
    một mẩu khung kẻ ô không chứa chữ nào của lý do thật, và log của Job là thứ
    duy nhất còn lại sau khi pod bị dọn.
    """
    return " ".join(text.split())[:limit]


def bucket_key(uri: str) -> str:
    """`s3://bucket/key` -> `bucket/key` — `pyarrow.fs` nhận đường dẫn KHÔNG
    scheme, còn `add_files` của PyIceberg thì đòi CÓ scheme. Hai API cạnh nhau,
    hai quy ước, và trộn nhầm cho ra `NoSuchKey` chứ không phải một lỗi nói rõ."""
    return uri.removeprefix("s3://")


def s3_filesystem(*, endpoint: str, access_key: str, secret_key: str) -> S3FileSystem:
    """`S3FileSystem` với credential GỐC MinIO — CHỈ dùng cho Q4b.

    Tách scheme khỏi host thay vì đưa cả `http://minio:9000` vào
    `endpoint_override`: pyarrow chấp nhận cả hai dạng, nhưng dạng tách rời
    không có trường hợp nào mơ hồ, và một endpoint đọc nhầm thành https cho ra
    một lỗi TLS chẳng nhắc gì tới cấu hình.
    """
    scheme, _, netloc = endpoint.partition("://")
    return S3FileSystem(
        access_key=access_key,
        secret_key=secret_key,
        endpoint_override=netloc,
        scheme=scheme,
        region="us-east-1",
        allow_bucket_creation=False,
    )


def write_parquet_via_catalog(table: Any, uri: str, data: pa.Table) -> None:
    """Ghi MỘT file Parquet qua credential mà catalog vend — đường của pod nạp.

    `pyarrow.parquet.write_table` vẫn là bên mã hoá Parquet; `table.io` chỉ cấp
    ống byte. Xem docstring đầu file, mục "Vì sao Parquet ... ghi qua table.io".

    `close()` trong `finally` chứ không phó mặc cho `write_table`: pyarrow chỉ
    đóng cái sink mà CHÍNH NÓ mở, nên một stream do người gọi đưa vào sẽ không
    được flush nếu không ai đóng — và một file Parquet thiếu footer đọc ra là
    "file quá ngắn", một câu không nhắc gì tới việc quên đóng.
    """
    out = table.io.new_output(uri).create(overwrite=True)
    try:
        pq.write_table(data, out)
    finally:
        out.close()


def parquet_field_ids(table: Any, uri: str) -> dict[str, str]:
    """Field ID của từng cột trong một file Parquet, đọc TỪ chính file đó.

    Trả `dict` tên cột -> field id (hoặc `"(không có)"`), để bên gọi in ra được
    bằng chứng chứ không chỉ một câu true/false.
    """
    stream = table.io.new_input(uri).open()
    try:
        schema = pq.ParquetFile(stream).schema_arrow
    finally:
        stream.close()
    found: dict[str, str] = {}
    for field in schema:
        metadata = field.metadata or {}
        raw = metadata.get(_PARQUET_FIELD_ID)
        found[field.name] = raw.decode() if raw is not None else "(không có)"
    return found


def any_data_file_uri(catalog: RestCatalog, identifier: str) -> str | None:
    """Đường dẫn của MỘT data file bất kỳ mà bảng đang tham chiếu.

    Dùng cho phép ĐỐI CHỨNG của Q4a: một file do chính Iceberg ghi phải CÓ field
    ID. Không có phép đối chứng đó thì "pyarrow thuần không ghi field ID" chỉ là
    một quan sát về một file, không phải một khác biệt giữa hai đường ghi.
    """
    for task in catalog.load_table(identifier).scan().plan_files():
        path = task.file.file_path
        return str(path)
    return None


@dataclass(frozen=True, slots=True)
class SnapshotAnswer:
    """Một dòng của Q1 — số snapshot là câu trả lời, số dòng là phép canh rằng
    snapshot đó có thật sự mang dữ liệu."""

    n_files: int
    snapshots_added: int
    rows_expected: int
    rows_read: int
    seconds: float

    @property
    def ok(self) -> bool:
        return self.snapshots_added == 1 and self.rows_read == self.rows_expected


def probe_q1(catalog: RestCatalog, namespace: str, *, n_files: int, rows: int) -> SnapshotAnswer:
    """CỬA CHẶN: N file Parquet qua MỘT `add_files` — bao nhiêu snapshot?

    Bảng mới, rỗng, cho mỗi N: đọc `before` chứ không giả định 0, phòng trường
    hợp `create_table` một ngày nào đó tự sinh một snapshot khởi tạo (ĐO 2 đã
    ghi cùng lập luận này).
    """
    identifier = f"{namespace}.q1_n{n_files}"
    schema_probe = make_batch(1, 0)
    catalog.create_table(identifier, schema=schema_probe.schema)
    table = catalog.load_table(identifier)

    paths: list[str] = []
    for i in range(n_files):
        data = make_batch(rows, i)
        uri = f"{table.location()}/data/q1-n{n_files}-{i:05d}.parquet"
        write_parquet_via_catalog(table, uri, data)
        paths.append(uri)

    before = snapshot_count(catalog, identifier)
    started = time.perf_counter()
    table.add_files(paths)
    seconds = time.perf_counter() - started
    after = snapshot_count(catalog, identifier)

    return SnapshotAnswer(
        n_files=n_files,
        snapshots_added=after - before,
        rows_expected=n_files * rows,
        rows_read=row_count(catalog, identifier),
        seconds=seconds,
    )


def work_append_path(
    catalog_factory: Callable[[], RestCatalog],
    *,
    identifier: str,
    rows_per_batch: int,
    batches: int,
) -> dict[str, Any]:
    """Đường HIỆN TẠI: một `append` (tức một commit catalog) cho MỖI lô.

    `catalog.load_table(...).append(...)` mỗi lô, KHÔNG giữ một `Table` dùng
    lại: đó đúng là điều `Lakehouse.append` làm (xem `icebergkit/lakehouse.py`),
    và vòng lặp nạp thật đi qua đúng hàm đó. Một `Table` giữ lại giữa các lô sẽ
    đo một đường mà production không chạy.
    """
    catalog = catalog_factory()
    schema_probe = make_batch(1, 0)
    catalog.create_table(identifier, schema=schema_probe.schema)
    del schema_probe

    started = time.perf_counter()
    rows = 0
    for i in range(batches):
        data = make_batch(rows_per_batch, i)
        rows += data.num_rows
        catalog.load_table(identifier).append(data)
        # Giữ ĐÚNG một lô sống tại một thời điểm — cùng hình dạng mà `IcebergSink`
        # chạy, và điều kiện để con số RSS nói về đường ghi chứ về vòng lặp này.
        del data
    seconds = time.perf_counter() - started

    return {
        "wall_seconds": seconds,
        "write_seconds": seconds,
        "commit_seconds": 0.0,
        "commits": batches,
        "rows": rows,
    }


def work_add_files_path(
    catalog_factory: Callable[[], RestCatalog],
    *,
    identifier: str,
    rows_per_batch: int,
    batches: int,
) -> dict[str, Any]:
    """Đường ĐỀ XUẤT: ghi N file Parquet theo luồng, rồi MỘT `add_files`.

    Hai đồng hồ tách rời, vì hai giai đoạn này chịu hai loại chi phí khác nhau
    và một tổng gộp sẽ giấu mất điều cần biết: giai đoạn ghi tỉ lệ với BYTE
    (mạng + mã hoá Parquet), giai đoạn commit là chi phí CỐ ĐỊNH mà cả phép thăm
    dò này tồn tại để cắt.
    """
    catalog = catalog_factory()
    schema_probe = make_batch(1, 0)
    catalog.create_table(identifier, schema=schema_probe.schema)
    del schema_probe
    table = catalog.load_table(identifier)

    started = time.perf_counter()
    paths: list[str] = []
    rows = 0
    for i in range(batches):
        data = make_batch(rows_per_batch, i)
        rows += data.num_rows
        uri = f"{table.location()}/data/part-{i:05d}.parquet"
        write_parquet_via_catalog(table, uri, data)
        del data
        paths.append(uri)
    write_seconds = time.perf_counter() - started

    commit_started = time.perf_counter()
    table.add_files(paths)
    commit_seconds = time.perf_counter() - commit_started

    return {
        "wall_seconds": write_seconds + commit_seconds,
        "write_seconds": write_seconds,
        "commit_seconds": commit_seconds,
        "commits": 1,
        "rows": rows,
    }


def measure_isolated(label: str, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Chạy `work` trong một tiến trình CON và mang về `ru_maxrss` của chính nó.

    Xem docstring đầu file, mục "Vì sao Q2 phải fork". `rss_at_fork_mib` được
    ghi lại Ở CHA ngay trước khi fork và đi kèm kết quả: nó là NỀN mà đỉnh của
    con đứng trên, và không in nó ra thì hai con số tuyệt đối không so được với
    bất cứ thứ gì.

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
        payload["rss_peak_mib"] = rss_mib()
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
        # thay vì để một `json.loads("")` ném `JSONDecodeError`, một câu không
        # gợi ý gì về việc vừa có một tiến trình bị giết.
        return {
            "error": f"tiến trình con {label!r} chết mà không gửi kết quả nào "
            "(nhiều khả năng bị giết: OOM hoặc tín hiệu)",
            "rss_at_fork_mib": at_fork,
        }
    result: dict[str, Any] = json.loads(raw)
    return result


def probe_q4a_field_ids(
    catalog: RestCatalog, namespace: str, *, rows: int, control_identifier: str | None
) -> dict[str, str]:
    """Q4a — field ID và name-mapping, đo ở CẢ BA chỗ có thể trả lời.

    1. File Parquet do pyarrow thuần ghi: có field ID không?
    2. Bảng TRƯỚC `add_files`: có name-mapping không?
    3. Bảng SAU `add_files`: có chưa, và ai đặt nó vào đó?

    Cộng một phép ĐỐI CHỨNG: một data file do chính Iceberg ghi (bảng của Q2)
    có field ID không? Không có vế này thì kết quả (1) không phân biệt được
    "pyarrow không ghi field ID" với "phép đo đọc nhầm chỗ".
    """
    identifier = f"{namespace}.q4a_name_mapping"
    schema_probe = make_batch(1, 0)
    catalog.create_table(identifier, schema=schema_probe.schema)
    table = catalog.load_table(identifier)

    uri = f"{table.location()}/data/q4a-00000.parquet"
    write_parquet_via_catalog(table, uri, make_batch(rows, 0))

    answers: dict[str, str] = {}
    answers["field id trong Parquet pyarrow"] = ", ".join(
        f"{name}={value}" for name, value in parquet_field_ids(table, uri).items()
    )

    control = "(không có bảng đối chứng)"
    if control_identifier is not None:
        control_uri = any_data_file_uri(catalog, control_identifier)
        if control_uri is None:
            control = "(bảng đối chứng không có data file nào)"
        else:
            # `io` của CHÍNH bảng đối chứng, không phải của bảng q4a. Lakekeeper
            # vend credential STS hẹp theo TỪNG BẢNG, nên đọc data file của bảng
            # A bằng `io` của bảng B trả về ACCESS_DENIED — đã ăn lỗi đó thật ở
            # lần chạy đầu. Đây cũng là dữ kiện đầu tiên của Q4b, và Q4b đo nó
            # thành một câu trả lời tường minh thay vì để nó là một tai nạn.
            control_table = catalog.load_table(control_identifier)
            control = ", ".join(
                f"{name}={value}"
                for name, value in parquet_field_ids(control_table, control_uri).items()
            )
    answers["field id trong Parquet do Iceberg ghi"] = control

    before = table.metadata.name_mapping()
    answers["name-mapping TRƯỚC add_files"] = (
        "(không có)" if before is None else f"có, {len(before.root)} field"
    )
    answers["thuộc tính bảng TRƯỚC"] = (
        "(không có)" if _NAME_MAPPING_PROPERTY not in table.properties else "có"
    )

    table.add_files([uri])

    reloaded = catalog.load_table(identifier)
    after = reloaded.metadata.name_mapping()
    answers["name-mapping SAU add_files"] = (
        "(không có)" if after is None else f"có, {len(after.root)} field"
    )
    answers["thuộc tính bảng SAU"] = (
        "(không có)"
        if _NAME_MAPPING_PROPERTY not in reloaded.properties
        else reloaded.properties[_NAME_MAPPING_PROPERTY][:120]
    )
    answers["dòng đọc lại được"] = f"{row_count(catalog, identifier)}/{rows}"
    return answers


def probe_q4b_placement(
    catalog: RestCatalog,
    namespace: str,
    *,
    rows: int,
    fs: S3FileSystem,
    bucket: str,
    warehouse_prefix: str,
    outside_prefix: str,
) -> dict[str, str]:
    """Q4b — file phải nằm ở đâu, tách làm HAI câu hỏi độc lập.

    Với mỗi trong ba vị trí: (i) credential mà catalog VEND có ghi vào đó được
    không, và (ii) `add_files` có ĐĂNG KÝ được một file đã nằm sẵn ở đó không.
    File dùng cho (ii) luôn được ghi bằng credential GỐC MinIO, nên câu trả lời
    của (ii) không bao giờ bị một lỗi 403 lúc ghi làm nhiễu.

    Mỗi vị trí một bảng riêng: một lần `add_files` thành công ở vị trí trước sẽ
    làm số dòng của lần sau không đọc được nữa như một phép canh độc lập.
    """
    answers: dict[str, str] = {}
    data = make_batch(rows, 0)

    # Chú thích kiểu TƯỜNG MINH cho danh sách: không có nó, mypy strict coi ba
    # lambda dưới đây là hàm không kiểu và từ chối lời gọi `uri_of(...)`.
    cases: list[tuple[str, Callable[[str], str]]] = [
        ("1 trong location của chính bảng", lambda loc: f"{loc}/data/q4b.parquet"),
        (
            "2 trong warehouse, NGOÀI bảng",
            lambda _loc: f"s3://{bucket}/{warehouse_prefix}/q4b-outside-table.parquet",
        ),
        (
            "3 NGOÀI warehouse",
            lambda _loc: f"s3://{bucket}/{outside_prefix}q4b-outside-warehouse.parquet",
        ),
    ]
    for case, uri_of in cases:
        identifier = f"{namespace}.q4b_case{case[0]}"
        schema_probe = make_batch(1, 0)
        catalog.create_table(identifier, schema=schema_probe.schema)
        table = catalog.load_table(identifier)
        uri = uri_of(table.location())

        # (i) credential VEND có ghi được vào chỗ này không?
        try:
            write_parquet_via_catalog(table, f"{uri}.vendtest", make_batch(1, 0))
        except Exception as exc:  # câu hỏi CHÍNH LÀ "lỗi gì, nếu có" — bắt rộng
            vend = f"KHÔNG ({type(exc).__name__})"
        else:
            vend = "được"

        # (ii) `add_files` có đăng ký được một file đã nằm sẵn ở đó không? Ghi
        # bằng credential GỐC để tách khỏi câu (i).
        pq.write_table(data, bucket_key(uri), filesystem=fs)
        try:
            table.add_files([uri])
        except Exception as exc:  # bắt rộng có chủ đích, xem trên
            # Phân biệt "Iceberg TỪ CHỐI vị trí này" với "credential không với
            # tới được vị trí này" — hai lý do dẫn tới hai thiết kế khác hẳn
            # nhau, và chỉ có nội dung lỗi mới tách được chúng.
            #
            # Phân loại trên chuỗi ĐẦY ĐỦ, cắt SAU: `ACCESS_DENIED` nằm ở CUỐI
            # thông báo của pyarrow, sau cả một key S3 dài, nên phân loại trên
            # chuỗi đã cắt sẽ dán nhãn "bị từ chối" cho đúng những vị trí có key
            # dài nhất. Đã thấy đúng như thế ở lần chạy trước.
            full = str(exc)
            kind = "credential không đọc được" if "ACCESS_DENIED" in full else "bị từ chối"
            register = f"KHÔNG, {kind} ({type(exc).__name__}: {one_line(full)})"
        else:
            try:
                read = row_count(catalog, identifier)
            except Exception as exc:  # đăng ký xong mà ĐỌC hỏng cũng là một câu trả lời
                register = f"đăng ký được nhưng ĐỌC hỏng ({type(exc).__name__}: {str(exc)[:80]})"
            else:
                register = f"được, đọc lại {read}/{rows} dòng"

        answers[case] = f"ghi bằng credential vend: {vend}  |  add_files: {register}"

    return answers


def probe_q4c_duplicate_check(
    catalog: RestCatalog, namespace: str, *, rows: int, many_files_identifier: str
) -> dict[str, str]:
    """Q4c — `check_duplicate_files` tốn gì, và tắt nó đi mất gì.

    Chi phí đo trên HAI bảng có số data file khác hẳn nhau, vì đó là điều đáng
    biết: phép kiểm chạy `table.inspect.data_files()`, tức là đọc MỌI manifest
    của bảng, nên giá của nó đi theo kích thước bảng chứ không theo số file đang
    thêm — và một bảng bronze thật thì lớn dần mãi.

    "Tắt có an toàn không" KHÔNG trả lời được bằng suy luận: đăng ký CÙNG một
    file hai lần với phép kiểm tắt, rồi ĐẾM dòng. Số dòng nhân đôi là câu trả
    lời, và nó là một lỗi im lặng — không có gì hỏng, chỉ có số liệu sai gấp đôi.
    """
    answers: dict[str, str] = {}
    big = catalog.load_table(many_files_identifier)
    big_files = len(list(big.scan().plan_files()))

    extra_on = f"{big.location()}/data/q4c-on.parquet"
    # Đo THẲNG cái việc mà phép kiểm làm — `table.inspect.data_files()` đọc mọi
    # manifest của bảng. Chỉ đo `add_files` đầu-cuối là không đủ: lần commit
    # (~0,8s, xem ĐO 3) lớn hơn hẳn phần chênh lệch, nên hiệu số hai lần gọi
    # chìm trong nhiễu và có thể ra ÂM — đã thấy đúng như thế ở lần chạy thử.
    #
    # Đồng hồ chỉ bọc `inspect.data_files()`, KHÔNG bọc `load_table`: một lần
    # chạy trước đo bảng lớn trên `Table` đã nạp sẵn (9 ms) và bảng nhỏ qua một
    # `load_table(...)` mới (259 ms), rồi kết luận rằng bảng NHỎ đắt hơn. Con số
    # đó đo một vòng REST tới catalog, không đo phép kiểm. Số manifest in kèm vì
    # đó mới là đại lượng mà phép kiểm quét, chứ không phải số data file.
    started = time.perf_counter()
    big.inspect.data_files()
    big_inspect = time.perf_counter() - started
    big_manifests = big.inspect.manifests().num_rows
    answers[f"inspect.data_files(): bảng {big_files} data file / {big_manifests} manifest"] = (
        f"{big_inspect:.3f}s (đây LÀ việc mà check_duplicate_files làm)"
    )

    extra_off = f"{big.location()}/data/q4c-off.parquet"
    write_parquet_via_catalog(big, extra_on, make_batch(rows, 900))
    write_parquet_via_catalog(big, extra_off, make_batch(rows, 901))

    started = time.perf_counter()
    catalog.load_table(many_files_identifier).add_files([extra_on], check_duplicate_files=True)
    on_seconds = time.perf_counter() - started

    started = time.perf_counter()
    catalog.load_table(many_files_identifier).add_files([extra_off], check_duplicate_files=False)
    off_seconds = time.perf_counter() - started

    answers[f"chi phí trên bảng {big_files} data file"] = (
        f"bật {on_seconds:.3f}s vs tắt {off_seconds:.3f}s (chênh {on_seconds - off_seconds:+.3f}s)"
    )

    small = f"{namespace}.q4c_small"
    schema_probe = make_batch(1, 0)
    catalog.create_table(small, schema=schema_probe.schema)
    small_table = catalog.load_table(small)
    first_uri = f"{small_table.location()}/data/q4c-small.parquet"
    write_parquet_via_catalog(small_table, first_uri, make_batch(rows, 0))
    started = time.perf_counter()
    small_table.add_files([first_uri], check_duplicate_files=True)
    small_seconds = time.perf_counter() - started
    answers["chi phí trên bảng 0 data file"] = f"bật {small_seconds:.3f}s"

    small_loaded = catalog.load_table(small)
    started = time.perf_counter()
    small_loaded.inspect.data_files()
    small_inspect = time.perf_counter() - started
    small_manifests = small_loaded.inspect.manifests().num_rows
    answers[f"inspect.data_files(): bảng 1 data file / {small_manifests} manifest"] = (
        f"{small_inspect:.3f}s"
    )

    # An toàn (1): phép kiểm BẬT có chặn được lần đăng ký thứ hai không?
    try:
        catalog.load_table(small).add_files([first_uri], check_duplicate_files=True)
    except Exception as exc:  # câu hỏi CHÍNH LÀ "chặn bằng cách nào" — bắt rộng
        answers["đăng ký lại cùng file, kiểm BẬT"] = f"bị chặn: {type(exc).__name__}"
    else:
        answers["đăng ký lại cùng file, kiểm BẬT"] = (
            f"KHÔNG bị chặn — {row_count(catalog, small)} dòng"
        )

    # An toàn (2): và khi TẮT?
    before_rows = row_count(catalog, small)
    try:
        catalog.load_table(small).add_files([first_uri], check_duplicate_files=False)
    except Exception as exc:  # bắt rộng có chủ đích, xem trên
        answers["đăng ký lại cùng file, kiểm TẮT"] = f"bị chặn: {type(exc).__name__}"
    else:
        after_rows = row_count(catalog, small)
        answers["đăng ký lại cùng file, kiểm TẮT"] = (
            f"KHÔNG bị chặn — {before_rows} -> {after_rows} dòng "
            f"({'NHÂN ĐÔI, im lặng' if after_rows == before_rows * 2 else 'xem số'})"
        )
    return answers


def probe_q4d_schema_mismatch(catalog: RestCatalog, namespace: str, *, rows: int) -> dict[str, str]:
    """Q4d — schema lệch thì ỒN hay IM.

    Ba kiểu lệch, ba bảng riêng, vì một lần `add_files` "im lặng thành công"
    sẽ làm bảng bẩn và làm hai phép sau không đọc được kết quả của chính chúng.

    Không dừng ở việc `add_files` có ném hay không: nếu nó KHÔNG ném thì đọc
    lại bảng ngay, vì "im lúc ghi rồi vỡ lúc đọc" là một hành vi khác hẳn "im
    và trả về dữ liệu sai", và cả hai đều khác "ồn". Ba trường hợp đó dẫn tới ba
    thiết kế khác nhau ở phía gọi.
    """
    cases: dict[str, pa.Table] = {
        "thừa một cột": make_batch(rows, 0).append_column(
            "extra", pa.array(["x"] * rows, type=pa.string())
        ),
        "thiếu một cột": make_batch(rows, 0).select(["id"]),
        "sai kiểu cột": pa.table(
            {
                "id": pa.array([str(i) for i in range(rows)], type=pa.string()),
                "pad": pa.array(["y" * 8] * rows, type=pa.string()),
            }
        ),
    }

    answers: dict[str, str] = {}
    for index, (label, data) in enumerate(cases.items()):
        identifier = f"{namespace}.q4d_case{index}"
        schema_probe = make_batch(1, 0)
        catalog.create_table(identifier, schema=schema_probe.schema)
        table = catalog.load_table(identifier)
        uri = f"{table.location()}/data/q4d-{index}.parquet"
        write_parquet_via_catalog(table, uri, data)

        try:
            table.add_files([uri])
        except Exception as exc:  # câu hỏi CHÍNH LÀ "ném gì, nếu có" — bắt rộng
            answers[label] = f"ỒN: {type(exc).__name__}: {one_line(str(exc))}"
            continue
        try:
            scanned = catalog.load_table(identifier).scan().to_arrow()
        except Exception as exc:  # bắt rộng có chủ đích, xem docstring
            answers[label] = f"IM lúc ghi, VỠ lúc đọc: {type(exc).__name__}: {one_line(str(exc))}"
        else:
            # Cột nào đọc ra TOÀN NULL: đó là hình dạng của một lần "im lặng
            # nhận" — dữ liệu vào được bảng nhưng một cột biến mất giá trị, và
            # không có gì trong đường ghi nói ra điều đó.
            all_null = [
                name
                for name in scanned.column_names
                if scanned.num_rows > 0 and scanned.column(name).null_count == scanned.num_rows
            ]
            answers[label] = f"IM — add_files nhận, đọc lại được {scanned.num_rows}/{rows} dòng" + (
                f", cột TOÀN NULL: {', '.join(all_null)}" if all_null else ""
            )
    return answers


def probe_q5_swap(catalog: RestCatalog, namespace: str, *, rows: int, files: int) -> dict[str, str]:
    """Q5 — `add_files` có ghép được với cú tráo ba bước của `full` không.

    Chuỗi tráo lấy nguyên từ `loom_task.sink`: `rename_target_away` ->
    `promote_staging` -> `drop_old_target`, ba lời gọi catalog. Câu hỏi thật
    KHÔNG phải "rename có chạy không" (ĐO 2 mục D đã trả lời rồi) mà là: dữ liệu
    do `add_files` đăng ký là những đường dẫn TUYỆT ĐỐI trỏ vào location của
    bảng STAGING, và `rename_table` chỉ đổi tên trong catalog chứ không di
    chuyển một byte nào. Nên sau cú tráo, bảng đích tham chiếu những file nằm
    dưới thư mục của một bảng KHÔNG CÒN TỒN TẠI.

    Đó là một trạng thái đọc được hay một quả bom hẹn giờ, và chỉ có đọc thật
    mới phân biệt được. Script in ra đường dẫn data file TRƯỚC và SAU cú tráo để
    người đọc thấy chúng không đổi, chứ không chỉ thấy một số dòng khớp.
    """
    answers: dict[str, str] = {}
    run_id = uuid.uuid4()
    target = f"{namespace}.q5_target"
    staging = f"{target}__staging_{run_id.hex}"
    old_target = f"{target}__old_{run_id.hex}"

    # Bảng đích với dữ liệu CŨ, nạp theo đường append thường — `full` bao giờ
    # cũng tráo lên một bảng đã tồn tại từ lần nạp trước.
    old_data = make_batch(rows, 500)
    catalog.create_table(target, schema=old_data.schema)
    catalog.load_table(target).append(old_data)

    # Bảng staging, nạp bằng add_files.
    schema_probe = make_batch(1, 0)
    catalog.create_table(staging, schema=schema_probe.schema)
    staging_table = catalog.load_table(staging)
    paths: list[str] = []
    expected_ids: set[int] = set()
    for i in range(files):
        data = make_batch(rows, i)
        expected_ids.update(data.column("id").to_pylist())
        uri = f"{staging_table.location()}/data/q5-{i:05d}.parquet"
        write_parquet_via_catalog(staging_table, uri, data)
        paths.append(uri)
    staging_table.add_files(paths)

    answers["snapshot của staging sau add_files"] = str(snapshot_count(catalog, staging))
    before_files = sorted(
        task.file.file_path for task in catalog.load_table(staging).scan().plan_files()
    )
    answers["data file TRƯỚC tráo (mẫu)"] = before_files[0] if before_files else "(không có)"
    staging_location = catalog.load_table(staging).location()

    try:
        catalog.rename_table(target, old_target)
        catalog.rename_table(staging, target)
        catalog.drop_table(old_target)
    except Exception as exc:  # câu hỏi CHÍNH LÀ "bước nào hỏng" — bắt rộng
        answers["chuỗi tráo ba bước"] = f"HỎNG: {type(exc).__name__}: {one_line(str(exc))}"
        return answers
    answers["chuỗi tráo ba bước"] = "chạy hết ba bước"

    # Vì sao dữ liệu vẫn đọc được sau cú tráo, dù file nằm dưới thư mục của một
    # bảng không còn tên: `rename_table` KHÔNG di chuyển location. Bảng đích sau
    # tráo MANG THEO location của staging, nên credential mà Lakekeeper vend cho
    # nó vẫn phủ đúng chỗ những file đó nằm. Đây là mắt xích nối Q4b (file phải
    # nằm trong location của chính bảng) với Q5 — không in ra thì hai kết quả
    # trông như mâu thuẫn nhau.
    answers["location bảng đích SAU tráo == location staging"] = str(
        catalog.load_table(target).location() == staging_location
    )

    try:
        scanned = catalog.load_table(target).scan().to_arrow()
    except Exception as exc:  # bắt rộng có chủ đích
        answers["đọc bảng đích sau tráo"] = f"HỎNG: {type(exc).__name__}: {one_line(str(exc))}"
        return answers

    got_ids = set(scanned.column("id").to_pylist())
    answers["đọc bảng đích sau tráo"] = (
        f"{scanned.num_rows} dòng; id khớp staging: {got_ids == expected_ids}; "
        f"còn sót id của bảng cũ: {bool(got_ids & set(old_data.column('id').to_pylist()))}"
    )
    after_files = sorted(
        task.file.file_path for task in catalog.load_table(target).scan().plan_files()
    )
    answers["data file SAU tráo (mẫu)"] = after_files[0] if after_files else "(không có)"
    answers["đường dẫn data file có đổi không"] = (
        "KHÔNG đổi — vẫn trỏ vào thư mục của bảng staging cũ"
        if before_files == after_files
        else "CÓ đổi"
    )
    return answers


def purge_s3_prefix(
    *, s3_endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str
) -> int:
    """Xoá THẲNG mọi object dưới `prefix` qua S3 API — xem "Dọn dẹp" ở docstring
    đầu file. Bản sao của cùng hàm trong `measure_ingest_pod.py`/
    `probe_iceberg_single_commit.py`: catalog không chạm data file trên MinIO."""
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


def print_block(title: str, answers: dict[str, str]) -> None:
    print(f"--- {title}", flush=True)
    for key, value in answers.items():
        print(f"    {key}: {value}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rows-per-batch",
        type=int,
        default=10_000,
        help="Số dòng/lô cho Q2/Q3 — mặc định khớp cấu hình C1 của ĐO 3 để so trực tiếp",
    )
    parser.add_argument(
        "--batches",
        type=int,
        default=50,
        help="Số lô cho Q2/Q3 — mặc định khớp ĐO 3 (50 lô x 10.000 dòng = 0,149 GB)",
    )
    parser.add_argument(
        "--q1-rows",
        type=int,
        default=1_000,
        help="Số dòng mỗi file cho Q1 — câu hỏi 'một hay nhiều snapshot' không phụ thuộc cỡ file",
    )
    parser.add_argument(
        "--probe-rows",
        type=int,
        default=1_000,
        help="Số dòng cho Q4/Q5 — cùng lý do như --q1-rows",
    )
    parser.add_argument("--q5-files", type=int, default=5)
    parser.add_argument("--bucket", default="loom-local")
    parser.add_argument(
        "--lakekeeper-url",
        default="http://loom-lakekeeper:8181",
        help="DNS nội bộ cụm — cùng lý do chạy TRONG CỤM đã ghi ở probe_iceberg_single_commit.py",
    )
    parser.add_argument("--minio-endpoint", default="http://minio:9000")
    parser.add_argument("--namespace", default="bench_add_files")
    args = parser.parse_args()

    # Credential GỐC MinIO — tiêm qua secretKeyRef trong Job spec (Secret
    # minio-root), xem measure_ingest_pod.py mục "Credential MinIO".
    access_key = os.environ["MINIO_ACCESS_KEY"]
    secret_key = os.environ["MINIO_SECRET_KEY"]

    management_url = args.lakekeeper_url
    catalog_uri = f"{management_url}/catalog"

    ensure_bootstrapped(management_url)

    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    key_prefix = prefix_for_lakehouse(workspace_id, lakehouse_id).rstrip("/")
    warehouse_name = f"probe-add-files-{uuid.uuid4().hex[:10]}"
    # NGOÀI prefix của warehouse, có chủ đích (Q4b vị trí 3), nên nó KHÔNG được
    # dọn bởi bước purge của warehouse — theo dõi riêng, purge riêng.
    outside_prefix = f"probe-add-files-outside/{uuid.uuid4().hex[:10]}/"

    warehouse_id = create_warehouse(
        management_url,
        name=warehouse_name,
        bucket=args.bucket,
        key_prefix=key_prefix,
        s3_endpoint=args.minio_endpoint,
        access_key=access_key,
        secret_key=secret_key,
    )
    print(f"warehouse: {warehouse_name} ({warehouse_id})  prefix={key_prefix}", flush=True)
    print(f"prefix NGOÀI warehouse (Q4b): {outside_prefix}", flush=True)

    def catalog_factory() -> RestCatalog:
        return build_catalog(
            catalog_uri=catalog_uri, warehouse=warehouse_name, s3_endpoint=args.minio_endpoint
        )

    catalog = catalog_factory()
    catalog.create_namespace_if_not_exists(args.namespace)

    id_append = f"{args.namespace}.q2_append_path"
    id_add_files = f"{args.namespace}.q2_add_files_path"

    try:
        # Q2/Q3 CHẠY TRƯỚC, và chạy liên tiếp: hai tiến trình con phải fork từ
        # một tiến trình cha ở cùng một trạng thái bộ nhớ, nếu không hai nền RSS
        # lệch nhau và phép so sánh mất nghĩa (xem docstring đầu file).
        print("", flush=True)
        print(
            f"=== Q2/Q3: {args.batches} lô x {args.rows_per_batch:,} dòng, HAI đường ===",
            flush=True,
        )
        append_result = measure_isolated(
            "append",
            lambda: work_append_path(
                catalog_factory,
                identifier=id_append,
                rows_per_batch=args.rows_per_batch,
                batches=args.batches,
            ),
        )
        print(f"    đường append   : {append_result}", flush=True)
        add_files_result = measure_isolated(
            "add_files",
            lambda: work_add_files_path(
                catalog_factory,
                identifier=id_add_files,
                rows_per_batch=args.rows_per_batch,
                batches=args.batches,
            ),
        )
        print(f"    đường add_files: {add_files_result}", flush=True)

        print("", flush=True)
        print("=== Q1 (CỬA CHẶN): N file Parquet -> bao nhiêu snapshot? ===", flush=True)
        q1_answers = [
            probe_q1(catalog, args.namespace, n_files=n, rows=args.q1_rows) for n in (1, 5, 20)
        ]
        for answer in q1_answers:
            print(
                f"    N={answer.n_files:2d} file -> {answer.snapshots_added} snapshot mới, "
                f"đọc lại {answer.rows_read}/{answer.rows_expected} dòng, "
                f"{answer.seconds:.3f}s",
                flush=True,
            )

        print("", flush=True)
        print("=== Q4: ràng buộc ===", flush=True)
        q4a = probe_q4a_field_ids(
            catalog, args.namespace, rows=args.probe_rows, control_identifier=id_append
        )
        print_block("Q4a field ID / name-mapping", q4a)

        fs = s3_filesystem(
            endpoint=args.minio_endpoint, access_key=access_key, secret_key=secret_key
        )
        q4b = probe_q4b_placement(
            catalog,
            args.namespace,
            rows=args.probe_rows,
            fs=fs,
            bucket=args.bucket,
            warehouse_prefix=key_prefix,
            outside_prefix=outside_prefix,
        )
        print_block("Q4b vị trí file", q4b)

        q4c = probe_q4c_duplicate_check(
            catalog, args.namespace, rows=args.probe_rows, many_files_identifier=id_add_files
        )
        print_block("Q4c check_duplicate_files", q4c)

        q4d = probe_q4d_schema_mismatch(catalog, args.namespace, rows=args.probe_rows)
        print_block("Q4d schema lệch", q4d)

        print("", flush=True)
        print("=== Q5: ghép với cú tráo ba bước của `full` ===", flush=True)
        q5 = probe_q5_swap(catalog, args.namespace, rows=args.probe_rows, files=args.q5_files)
        print_block("Q5", q5)

        # ---- TỔNG KẾT ----
        passed = all(answer.ok for answer in q1_answers)
        print("", flush=True)
        print("=== TỔNG KẾT ===", flush=True)
        for answer in q1_answers:
            print(
                f"Q1  N={answer.n_files:2d} -> {answer.snapshots_added} snapshot"
                f"{'' if answer.ok else '   <-- KHÔNG ĐẠT'}",
                flush=True,
            )
        if "error" in append_result or "error" in add_files_result:
            print("Q2/Q3: một trong hai đường hỏng — xem dòng in ở trên", flush=True)
        else:
            print(
                f"Q2  RSS đỉnh: append {append_result['rss_peak_mib']:,.0f} MiB "
                f"(nền {append_result['rss_at_fork_mib']:,.0f}) vs "
                f"add_files {add_files_result['rss_peak_mib']:,.0f} MiB "
                f"(nền {add_files_result['rss_at_fork_mib']:,.0f})",
                flush=True,
            )
            print(
                f"Q3  thời gian: append {append_result['wall_seconds']:.1f}s "
                f"({append_result['commits']} commit) vs add_files "
                f"{add_files_result['wall_seconds']:.1f}s "
                f"(ghi {add_files_result['write_seconds']:.1f}s + commit "
                f"{add_files_result['commit_seconds']:.1f}s, "
                f"{add_files_result['commits']} commit)",
                flush=True,
            )
        print("", flush=True)
        if passed:
            print(
                "ĐẠT — add_files hạ N file vào ĐÚNG 1 snapshot ở cả ba cỡ N, và dữ liệu "
                "đọc lại đủ. Nút thắt 'một commit mỗi lô' KHÔNG còn là ràng buộc của "
                "PyIceberg 0.11.1; nó là ràng buộc của cách đường nạp đang gọi thư viện.",
                flush=True,
            )
            print(
                "      ĐỌC Q4 TRƯỚC KHI THIẾT KẾ: mã thoát này chỉ nói về Q1. Ràng buộc "
                "vị trí file, giá của check_duplicate_files và hành vi khi schema lệch "
                "quyết định đường nạp phải viết ra sao, và chúng KHÔNG có mặt trong mã "
                "thoát.",
                flush=True,
            )
        else:
            print(
                "KHÔNG ĐẠT — add_files KHÔNG gộp N file vào một snapshot. Hướng "
                "'ghi theo luồng rồi commit một lần' đóng lại ở PyIceberg 0.11.1, "
                "đúng như transaction() đã đóng ở ĐO 2.",
                flush=True,
            )
        return 0 if passed else 1
    finally:
        # Best-effort, từng bước một, KHÔNG BAO GIỜ ném tiếp — xem mục "Dọn dẹp"
        # ở docstring đầu file cho cả ba lý do.
        cleanup_errors: list[str] = []

        try:
            existing = [".".join(identifier) for identifier in catalog.list_tables(args.namespace)]
        except Exception as exc:  # bắt rộng có chủ đích — dọn dẹp không được ném
            existing = []
            cleanup_errors.append(f"list_tables({args.namespace}): {type(exc).__name__}: {exc}")

        for identifier in existing:
            try:
                catalog.drop_table(identifier)
            except Exception as exc:  # bắt rộng có chủ đích
                cleanup_errors.append(f"drop_table({identifier}): {type(exc).__name__}: {exc}")

        try:
            catalog.drop_namespace(args.namespace)
        except Exception as exc:  # bắt rộng có chủ đích
            cleanup_errors.append(f"drop_namespace({args.namespace}): {type(exc).__name__}: {exc}")

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.delete(f"{management_url}/management/v1/warehouse/{warehouse_id}")
            if resp.status_code not in (204, 404):
                resp.raise_for_status()
        except Exception as exc:  # bắt rộng có chủ đích
            cleanup_errors.append(f"delete warehouse {warehouse_id}: {type(exc).__name__}: {exc}")

        deleted = 0
        for prefix in (key_prefix, outside_prefix):
            try:
                deleted += purge_s3_prefix(
                    s3_endpoint=args.minio_endpoint,
                    access_key=access_key,
                    secret_key=secret_key,
                    bucket=args.bucket,
                    prefix=prefix,
                )
            except Exception as exc:  # bắt rộng có chủ đích
                cleanup_errors.append(f"purge_s3_prefix({prefix}): {type(exc).__name__}: {exc}")

        print("", flush=True)
        print(
            f"đã dọn: {len(existing)} bảng (liệt kê thật lúc dọn, không theo tên đã tạo) "
            f"+ namespace + warehouse, {deleted} object S3 dưới {key_prefix} và "
            f"{outside_prefix}",
            flush=True,
        )
        for err in cleanup_errors:
            print(f"CẢNH BÁO DỌN DẸP (không đổi verdict/mã thoát ở trên): {err}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
