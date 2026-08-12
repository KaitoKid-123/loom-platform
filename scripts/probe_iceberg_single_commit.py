"""Đo 2 của Giai đoạn 3a — PyIceberg 0.11.1 có gộp nhiều lần ghi thành MỘT
snapshot trong một transaction hay không, và (SAU KHI câu trả lời hoá ra là
KHÔNG) `rename_table` có dựng nổi một chuỗi tráo bảng thay thế hay không, TỪ
TRONG CỤM.

## LỊCH SỬ — A/B/C KHÔNG CÒN LÀ CỬA CHẶN

Vòng đo đầu (A/B/C dưới đây) đã chạy thật lên Lakekeeper thật và trả lời XONG:
transaction của PyIceberg 0.11.1 KHÔNG gộp nhiều lần ghi thành một snapshot —
xem "KẾT QUẢ VÒNG A/B/C ĐÃ CHỐT" bên dưới. Thiết kế `full` một-transaction
(Task 12 bản gốc) đã bị BÁC BỎ dựa trên số đo đó, và chủ dự án đã chọn hướng
thay thế: bảng tạm (staging) rồi tráo tên qua `catalog.rename_table`. A/B/C vẫn
CHẠY THẬT mỗi lần gọi script (không hard-code số cũ vào code — số đo phải luôn
đến từ một lần chạy thật, đúng tinh thần "đo, không suy đoán" của cả Đo 2), NHƯNG
kết quả của chúng KHÔNG còn quyết định mã thoát — xem "D" bên dưới mới là CỬA
CHẶN SỐNG. In riêng dưới nhãn "GHI NHẬN LỊCH SỬ" để không ai đọc nhầm là cửa chặn
đang hiệu lực.

## Vì sao phép đo A/B/C tồn tại (đã trả lời xong, giữ lại làm hồ sơ)

Thiết kế `mode: full` (spec Giai đoạn 3a mục 3.1, Task 12) đọc TOÀN BỘ bảng
nguồn rồi THAY bảng bronze một cách NGUYÊN TỬ: ghi Parquet ra S3 theo từng lô để
RAM không phình theo cỡ bảng, nhưng chỉ có ĐÚNG MỘT lần commit catalog ở cuối —
lô đầu thay dữ liệu cũ, các lô sau nối thêm, và nếu tiến trình chết TRƯỚC lần
commit đó thì bảng cũ đứng nguyên. Toàn bộ thiết kế đó dựa trên MỘT giả định
chưa kiểm: PyIceberg 0.11.1 có thể gom nhiều `tx.append`/`tx.overwrite` vào một
`Transaction` rồi đóng gói thành MỘT snapshot Iceberg duy nhất khi gọi
`commit_transaction()`.

Đã ĐỌC mã nguồn `pyiceberg/table/__init__.py` và
`pyiceberg/table/update/snapshot.py` (bản 0.11.1 cài trong workspace này)
trước khi viết script: mỗi `tx.append(...)`/`tx.overwrite(...)` dựng một
`_SnapshotProducer` RIÊNG, và `__exit__` của nó GỌI NGAY `commit()` ->
`transaction._apply(...)` — nghĩa là mỗi lời gọi như vậy XẾP THÊM một
`AddSnapshotUpdate` (một `Snapshot` với `snapshot_id` mới, `parent_snapshot_id`
trỏ về snapshot trước) vào `Transaction._updates`, TRƯỚC KHI có bất kỳ request
mạng nào. `Transaction.commit_transaction()` mới gửi request DUY NHẤT
(`Table._do_commit`) mang TOÀN BỘ `_updates` đã xếp. Vậy "một transaction" ở
tầng PyIceberg đảm bảo "một request PUT, một điều kiện tranh chấp lạc quan" —
NHƯNG không có gì trong đọc mã nguồn cho thấy nó gộp NHIỀU `AddSnapshotUpdate`
thành MỘT snapshot; có vẻ nó xếp CHỒNG nhiều snapshot rồi gửi chung một request.
Đọc mã không phải bằng chứng — spec Giai đoạn 2 đã trả giá nhiều lần cho việc
tin tài liệu/mã nguồn thay vì đo — nên đây là lý do phép đo này PHẢI chạy thật
lên Lakekeeper thật rồi đếm `len(table.snapshots())`, con số duy nhất "không
nói dối" (xem yêu cầu Task 2 GĐ3a).

## Vì sao KHÔNG qua `Lakehouse` (icebergkit)

`Lakehouse.append`/`create_from` (xem `packages/icebergkit/src/loom_iceberg/
lakehouse.py`) mỗi lời gọi tự mở và tự đóng một transaction của riêng nó
(`self._catalog.load_table(qualified).append(data)` — bên trong PyIceberg lại
là `with self.transaction() as tx: tx.append(df)`, xem `pyiceberg/table/
__init__.py`). Không có API nào trong `Lakehouse` giữ một transaction MỞ qua
nhiều lời gọi — đúng thứ ba phép đo A/B/C dưới đây cần. Script này đi THẲNG vào
`RestCatalog`/`Table.transaction()` của PyIceberg, giống cách
`scripts/measure_write_path.py` tách giai đoạn ghi/commit.

## Vì sao chạy TRONG CỤM (như `measure_ingest_pod.py`), không port-forward như
`measure_write_path.py`

Hai tiền lệ đã có trong repo — script này CHỌN đường của `measure_ingest_pod.py`:
chạy như một Job trong cụm thì `http://minio:9000`/`http://loom-lakekeeper:8181`
phân giải thẳng qua DNS nội bộ Kubernetes, không cần giả lập địa chỉ gateway
docker cho STS AssumeRole như `measure_write_path.py` phải làm khi chạy từ HOST
(xem "Cạm bẫy mạng" trong docstring của script đó). Đường trong cụm ĐÃ ĐƯỢC
CHỨNG MINH chạy được ở Đo 1 (`measure_ingest_pod.py`) nên không có rủi ro mạng
mới nào cần dò lại ở đây.

## Ba phép đo A/B/C (lịch sử — xem D bên dưới cho cửa chặn đang hiệu lực)

  A. Hai `tx.append` trong MỘT transaction, trên một bảng RỖNG mới tạo (0
     snapshot). Đếm `len(table.snapshots())` trước/sau — số đó CHÍNH LÀ số
     snapshot mà transaction này sinh ra, vì bảng khởi đầu 0 snapshot.

  B. Đúng hình dạng `full` cần: một bảng ĐÃ CÓ dữ liệu cũ (mô phỏng bronze đã
     tồn tại — nạp bằng MỘT `table.append` riêng, commit ngay, không nằm trong
     phép đo), rồi trong MỘT transaction mới: `tx.overwrite(lô_mới_1)` (thay
     TOÀN BỘ nội dung cũ) + `tx.append(lô_mới_2)` (nối thêm). Đếm snapshot sinh
     thêm SAU khi bảng đã có dữ liệu cũ — không tính snapshot của bước nạp dữ
     liệu cũ.

     A và B dùng lô NHỎ (`--snapshot-probe-rows`, mặc định 1000): câu hỏi "một
     hay nhiều snapshot" không phụ thuộc số dòng, và lô nhỏ giữ Job chạy nhanh.

  C. RSS đỉnh của tiến trình khi giữ MỘT transaction mở qua NHIỀU lô liên tiếp
     (`--rows-per-batch`/`--batches-c`, mặc định giống Đo 1 — 200 000 dòng/lô,
     20 lô — để so sánh trực tiếp với con số đã đo ở đó). Nếu transaction
     GIỮ dữ liệu trong RAM tới lúc commit thay vì ghi file Parquet ngay trong
     mỗi `tx.append`, RSS sẽ CÒN TỆ HƠN Đo 1 (vốn đã leo dần lên ~406 MiB không
     phẳng, xem `Makefile` mục `measure-ingest-pod`): RAM sẽ tỉ lệ với CẢ BẢNG
     thay vì với MỘT lô. `resource.getrusage(RUSAGE_SELF).ru_maxrss` in ra sau
     mỗi lô — đỉnh KHÔNG BAO GIỜ giảm, nên chuỗi in ra tự nó cho biết còn leo
     hay đã phẳng.

     C chạy SAU A/B trong cùng một tiến trình: `ru_maxrss` là đỉnh CỘNG DỒN từ
     lúc tiến trình khởi động, nên con số tuyệt đối của C đã include cả A/B —
     nhưng A/B dùng lô nhỏ (xem trên) nên đóng góp không đáng kể, và câu hỏi
     "có leo theo số lô của C không" chỉ cần nhìn ĐỘ DỐC trong chính vòng lặp
     của C, không cần tách rời tiến trình.

## KẾT QUẢ VÒNG A/B/C ĐÃ CHỐT (đo thật 2026-08-11/12, cụm k3d-loom)

    A: hai append trong một transaction       -> 2 snapshot
    B: overwrite+append trong một transaction -> 3 snapshot
    C: RSS đỉnh, 20 lô x 200 000 dòng, một transaction: 499 MiB (bò lên đều)

KHÔNG ĐẠT. Hai điều đáng ghi thêm vì không tự lộ ra chỉ từ ba con số trên:

  - B = 3 và A xác nhận 1 `tx.append` = 1 snapshot (A: 2 append = 2 snapshot,
    tuyến tính) => `tx.overwrite` MỘT MÌNH đã là 3 - 1 = 2 snapshot (một
    DELETE rồi một APPEND, xem `_OverwriteFiles`/`Transaction.overwrite` gọi
    `self.delete(...)` trước khi tự thêm data file mới trong
    `pyiceberg/table/__init__.py`). Suy ra TỪ A và B, không đo riêng — nhưng hệ
    quả thật: `overwrite()` KHÔNG atomic dù gọi MỘT MÌNH ngoài transaction nào
    cũng vậy — có một khoảnh khắc giữa hai snapshot mà bảng ĐỌC ĐƯỢC là RỖNG.
    Điều này tự nó loại phương án "chỉ cần overwrite() một lần" như một cách
    né chuyện gộp snapshot.

  - C = 499 MiB TỆ HƠN cách ghi-commit-từng-lô mà Đo 1 đã đo cho ĐÚNG hình
    dạng 20 lô x 200 000 dòng đó: 406 MiB (xem `Makefile` mục
    `measure-ingest-pod`, dòng "rss_peak_mib=406"). Giữ transaction mở suốt
    quá trình ghi không chỉ THẤT BẠI ở việc gộp snapshot — nó còn tốn RAM hơn
    (499 so với 406 MiB, +93 MiB) so với việc commit ngay sau mỗi lô. Không có
    góc nào để phương án transaction "gần đạt": vừa sai giả định nền tảng, vừa
    tệ hơn hiện trạng.

## Phép đo D — CỬA CHẶN SỐNG: `rename_table` có dựng nổi một chuỗi tráo bảng?

Chủ dự án đã chọn hướng thay thế cho `full`: ghi vào một bảng TẠM (staging),
rồi TRÁO nó vào tên bảng bronze thật qua `catalog.rename_table`. `hasattr`
xác nhận `rename_table` TỒN TẠI trên `RestCatalog` 0.11.1 — nhưng "tồn tại
trong API" CHÍNH LÀ loại bằng chứng vừa làm giả định A/B/C sai (transaction
API cũng "tồn tại" và trông transactional). Bốn câu hỏi dưới đây PHẢI được
trả lời bằng cách gọi thật lên Lakekeeper thật, đúng thứ tự, không suy đoán:

  D1. `catalog.rename_table(from, to)` có chạy được qua Lakekeeper không?
      (tạo bảng, ghi dòng thật, rename.)

  D2. Dữ liệu có NGUYÊN VẸN dưới tên mới không? (đọc lại, so khớp từng dòng —
      không chỉ so tên bảng có phân giải được hay không.)

  D3. Tên CŨ có ngừng phân giải không? (kỳ vọng `NoSuchTableError` — nếu tên
      cũ vẫn còn, `rename_table` là COPY chứ không phải MOVE, một giả định
      khác không được coi là hiển nhiên.)

  D4. CÂU QUYẾT ĐỊNH: rename ĐÈ lên một tên ĐÃ TỒN TẠI (đúng thao tác tráo bảng
      cuối cùng — thay `t_target` bằng nội dung của `t_staging`) có được không?
      Tạo `t_target` (có dữ liệu), tạo `t_staging` (dữ liệu KHÁC), rồi
      `rename_table(t_staging, t_target)`.

      - THÀNH CÔNG và THAY dữ liệu: chuỗi tráo là MỘT bước
        `rename(staging -> target)` — nguyên tử THẬT (Lakekeeper coi rename là
        một thao tác catalog nguyên tử, target biến mất và được thay cùng lúc).
      - THẤT BẠI (vd. `TableAlreadyExistsError`): chuỗi tráo phải qua NHIỀU
        bước, để lại một cửa sổ không-nguyên-tử — HOẶC `drop(target)` rồi
        `rename(staging -> target)` (cửa sổ: bảng KHÔNG TỒN TẠI), HOẶC ba bước
        `rename(target -> target_old)`, `rename(staging -> target)`,
        `drop(target_old)` (cửa sổ hẹp hơn: giữa hai lời gọi catalog nhanh).
        Script này CHỈ báo cáo trường hợp nào xảy ra thật — KHÔNG tự chọn giữa
        hai chuỗi thay thế đó, đó là quyết định của chủ dự án.

D1-D3 quyết định MÃ THOÁT (xem `main`): nếu `rename_table` không chạy được,
không giữ nguyên dữ liệu, hoặc không thật sự MOVE tên, thì cả hướng thay thế
"bảng tạm + tráo tên" CŨNG không khả thi — một phát hiện còn nghiêm trọng hơn
A/B/C, vì lúc đó không còn phương án dự phòng hiển nhiên nào nữa. D4 KHÔNG
quyết định mã thoát — nó quyết định CHUỖI THAO TÁC nào `full` phải dùng, và
cả hai kết quả (đè được hay không) đều để lại một thiết kế khả thi.

## KẾT QUẢ D ĐÃ GHI NHẬN (đo thật 2026-08-12, cụm k3d-loom — D vẫn CHẠY THẬT
mỗi lần gọi script, số dưới đây là để đối chiếu, KHÔNG phải giá trị hard-code)

    D1 rename chạy được   : True
    D2 dữ liệu nguyên vẹn : True  (1000/1000 dòng khớp CẢ id lẫn pad)
    D3 tên cũ biến mất    : True  (NoSuchTableError — đúng là MOVE)
    D4 rename đè tên cũ   : conflict  (TableAlreadyExistsError — bị TỪ CHỐI)

ĐẠT (có điều kiện). D3 xác nhận `rename_table` là MOVE thật (không phải copy);
D4 xác nhận nó TỪ CHỐI ghi đè lên một tên đã tồn tại. Cộng hai sự thật đó lại:
chuỗi tráo bảng của `full` KHÔNG THỂ là một lời gọi `rename` duy nhất — nó phải
qua NHIỀU bước catalog (drop rồi rename, hoặc rename-away/rename-in/drop), và
CẢ HAI chuỗi đó đều để lại một cửa sổ mà tên bảng target KHÔNG PHÂN GIẢI được.
`full` do đó chỉ GẦN nguyên tử, KHÔNG nguyên tử tuyệt đối — spec Giai đoạn 3a
KHÔNG được hứa một đảm bảo mà nó không có. Chọn GIỮA hai chuỗi thay thế (2 bước
hay 3 bước) là quyết định của chủ dự án, không phải của phép đo này.

## Dọn dẹp

Bảy bảng (`probe_a`/`probe_b`/`probe_c`/`probe_d_src`/`probe_d_dst`/
`probe_d_target`/`probe_d_staging`) + một namespace + một warehouse dùng
CHUNG, dọn hết trong `finally` — đúng "CẠM BẪY DỌN DẸP" đã đo ở Giai đoạn 2c và
nhắc lại ở Đo 1: `drop_table`/`drop_namespace`/xoá warehouse KHÔNG xoá object
trên S3, phải `purge_s3_prefix` xoá THẲNG qua S3 API mới giải phóng đĩa. Gọi
THẲNG `catalog.drop_table`/`catalog.drop_namespace` (PyIceberg gốc), KHÔNG qua
`Lakehouse.drop_table`/`drop_namespace`: image `loom-query:dev` đang chạy bake
một bản `icebergkit` CŨ chưa có hai phương thức đó trên `Lakehouse` — Đo 1 đã
ăn `AttributeError` đúng chỗ này.

CẠM BẪY MỚI (ăn thật ở lần chạy có D): dọn theo danh sách TÊN ĐÃ TẠO (id_a,
id_b, ..., id_d_src...) sẽ cố `drop_table` một tên D1 đã RENAME ĐI — Lakekeeper
trả 403 Forbidden (không phải 404) cho một tên không còn tồn tại, vì management
API của nó CỐ Ý không phân biệt "không tìm thấy" với "không được phép" cho một
principal ẩn danh (tránh rò rỉ sự tồn tại của một bảng cho người không có
quyền). PyIceberg ném `ForbiddenError`, KHÔNG phải `NoSuchTableError`, nên một
`except NoSuchTableError` không bắt được nó. `main` sửa bằng cách liệt kê THẬT
qua `catalog.list_tables` lúc dọn (không theo tên đã tạo) và bọc MỌI bước dọn
dẹp (drop bảng, drop namespace, xoá warehouse, purge S3) trong try/except
RIÊNG — dọn dẹp là best-effort chạy SAU KHI verdict đã chốt, và một lỗi dọn dẹp
(dù là gì) KHÔNG được phép đổi mã thoát của một phép đo đã tính đúng.

Chạy: `make probe-single-commit` (dựng Job trong cụm, `backoffLimit: 0` — thiếu
cờ này Kubernetes tự thử lại và để lại HAI warehouse rác, đúng bẫy Đo 1 đã ăn).
"""

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from resource import RUSAGE_SELF, getrusage
from typing import TYPE_CHECKING

import boto3
import httpx
import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError

from loom_iceberg import build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse

if TYPE_CHECKING:
    # Xem chú thích TYPE_CHECKING tương tự trong measure_ingest_pod.py: image
    # loom-query build bằng `uv sync --frozen --no-dev` nên KHÔNG có
    # `mypy_boto3_s3` lúc chạy trong pod, dù có sẵn trên host qua `uv run`.
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef


def make_batch(rows: int, batch_index: int) -> pa.Table:
    """~268 byte/dòng, KHÁC NHAU từng dòng — bản sao nguyên xi từ
    `measure_ingest_pod.py` (đã đo đúng ở Đo 1, không có lý do dựng lại).

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
    """Tải lại bảng từ catalog rồi đếm snapshot — PHẢI tải lại (không dùng một
    `Table` đã giữ từ trước), vì `commit_transaction()` cập nhật metadata trên
    đối tượng `Table` gốc tại chỗ nhưng con số đáng tin là con số catalog THẬT
    SỰ đã lưu, không phải bộ nhớ cục bộ của client."""
    return len(catalog.load_table(identifier).snapshots())


def probe_a(catalog: RestCatalog, identifier: str, rows: int) -> int:
    """Hai `tx.append` trong MỘT transaction, trên bảng RỖNG mới tạo.

    Trả về số snapshot MỚI sinh ra (bảng khởi đầu 0 snapshot nên đây chính là
    tổng snapshot sau transaction — không cần trừ before/after riêng, nhưng
    vẫn đọc `before` để không ngầm giả định 0, phòng trường hợp `create_table`
    một ngày nào đó tự sinh snapshot khởi tạo).
    """
    batch1 = make_batch(rows, 0)
    catalog.create_table(identifier, schema=batch1.schema)
    before = snapshot_count(catalog, identifier)

    table = catalog.load_table(identifier)
    batch2 = make_batch(rows, 1)
    with table.transaction() as tx:
        tx.append(batch1)
        tx.append(batch2)

    after = snapshot_count(catalog, identifier)
    return after - before


def probe_b(catalog: RestCatalog, identifier: str, rows: int) -> int:
    """`tx.overwrite` (thay TOÀN BỘ bảng) rồi `tx.append` trong MỘT
    transaction — đúng hình dạng `mode: full` cần (Task 12).

    Nạp dữ liệu CŨ bằng một `table.append` riêng TRƯỚC, commit ngay (một
    transaction của chính nó, xem `Table.append` trong pyiceberg/table/
    __init__.py: `with self.transaction() as tx: tx.append(df)`) — bảng phải
    CÓ dữ liệu để `overwrite` có gì mà xoá, nếu không phép đo sẽ đo nhầm
    trường hợp "overwrite bảng rỗng", tình huống `full` không bao giờ gặp
    (bronze bao giờ cũng đã tồn tại từ lần ingest trước).
    """
    old = make_batch(rows, 900)
    catalog.create_table(identifier, schema=old.schema)
    table = catalog.load_table(identifier)
    table.append(old)  # snapshot của dữ liệu CŨ — KHÔNG tính vào phép đo này

    before = snapshot_count(catalog, identifier)
    table = catalog.load_table(identifier)  # tải lại: lấy đúng metadata sau commit trên
    batch_new1 = make_batch(rows, 901)
    batch_new2 = make_batch(rows, 902)
    with table.transaction() as tx:
        tx.overwrite(batch_new1)
        tx.append(batch_new2)

    after = snapshot_count(catalog, identifier)
    return after - before


def probe_c(
    catalog: RestCatalog, identifier: str, rows_per_batch: int, batches: int
) -> list[float]:
    """Giữ MỘT transaction mở qua `batches` lô liên tiếp, in RSS đỉnh sau mỗi
    lô. Trả danh sách RSS đỉnh (MiB) theo từng lô — người gọi tự đọc xu hướng.
    """
    first = make_batch(rows_per_batch, 0)
    table = catalog.create_table(identifier, schema=first.schema)

    peaks: list[float] = []
    with table.transaction() as tx:
        for i in range(batches):
            batch = first if i == 0 else make_batch(rows_per_batch, i)
            tx.append(batch)
            del batch
            peak = getrusage(RUSAGE_SELF).ru_maxrss / 1024
            peaks.append(peak)
            print(f"[C lô {i + 1:3d}/{batches}] RSS đỉnh={peak:,.0f} MiB", flush=True)
    return peaks


@dataclass(frozen=True, slots=True)
class ProbeDResult:
    """Bốn câu trả lời của D — mỗi trường mang cả kết quả VÀ bằng chứng bằng
    chữ, vì D là cửa chặn sống: người đọc log sau này phải thấy được TẠI SAO,
    không chỉ đúng/sai."""

    q1_rename_ok: bool
    q1_detail: str
    q2_data_intact: bool
    q2_detail: str
    q3_old_name_gone: bool
    q3_detail: str
    q4_case: str  # "replace_ok" | "conflict" | "unexpected"
    q4_detail: str


def probe_d(catalog: RestCatalog, namespace: str, rows: int) -> ProbeDResult:
    """Bốn câu hỏi về `rename_table` qua Lakekeeper THẬT, đúng thứ tự chủ dự
    án yêu cầu. Xem "Phép đo D" ở docstring đầu file cho lý do KHÔNG được suy
    đoán từ `hasattr(catalog, "rename_table")`: đó đúng là loại bằng chứng đã
    làm giả định A/B/C sai.
    """
    id_src = f"{namespace}.probe_d_src"
    id_dst = f"{namespace}.probe_d_dst"

    src_data = make_batch(rows, 700)
    catalog.create_table(id_src, schema=src_data.schema)
    catalog.load_table(id_src).append(src_data)

    # D1: rename_table chạy được qua Lakekeeper không?
    try:
        catalog.rename_table(id_src, id_dst)
    except Exception as exc:  # câu hỏi D1 CHÍNH LÀ "lỗi gì, nếu có" — bắt rộng có chủ đích
        q1_rename_ok = False
        q1_detail = f"{type(exc).__name__}: {exc}"
    else:
        q1_rename_ok = True
        q1_detail = f"rename_table({id_src!r}, {id_dst!r}) thành công"

    # D2: dữ liệu có nguyên vẹn dưới tên mới không? Chỉ kiểm được nếu D1 qua —
    # so khớp CẢ HAI cột theo cặp (id, pad) sau khi sắp xếp, không chỉ đếm
    # dòng: đếm đúng nhưng nội dung sai (vd. hoán đổi cột) vẫn phải bắt được.
    q2_data_intact = False
    q2_detail = "bỏ qua — D1 đã thất bại"
    if q1_rename_ok:
        try:
            scanned = catalog.load_table(id_dst).scan().to_arrow()
            got_ids = scanned.column("id").to_pylist()
            got_pads = scanned.column("pad").to_pylist()
            want_ids = src_data.column("id").to_pylist()
            want_pads = src_data.column("pad").to_pylist()
            got = sorted(zip(got_ids, got_pads, strict=True))
            want = sorted(zip(want_ids, want_pads, strict=True))
            q2_data_intact = got == want
            q2_detail = (
                f"{len(got)}/{len(want)} dòng khớp CẢ id lẫn pad"
                if q2_data_intact
                else f"LỆCH: đọc lại {len(got)} dòng dưới tên mới, gốc có {len(want)} dòng"
            )
        except Exception as exc:  # bắt rộng có chủ đích — Q2 CHÍNH LÀ "lỗi gì, nếu có"
            q2_detail = f"đọc bảng dưới tên mới lỗi: {type(exc).__name__}: {exc}"

    # D3: tên cũ có ngừng phân giải không? (rename = MOVE hay COPY?)
    q3_old_name_gone = False
    q3_detail = "bỏ qua — D1 đã thất bại"
    if q1_rename_ok:
        try:
            catalog.load_table(id_src)
        except NoSuchTableError:
            q3_old_name_gone = True
            q3_detail = "load_table(tên cũ) ném NoSuchTableError đúng như kỳ vọng (MOVE)"
        else:
            q3_detail = "tên cũ VẪN còn phân giải được — rename có vẻ là COPY, không phải MOVE"

    # D4: câu quyết định — rename ĐÈ lên một tên ĐÃ TỒN TẠI có được không?
    id_target = f"{namespace}.probe_d_target"
    id_staging = f"{namespace}.probe_d_staging"
    target_data = make_batch(rows, 800)
    staging_data = make_batch(rows, 801)
    catalog.create_table(id_target, schema=target_data.schema)
    catalog.load_table(id_target).append(target_data)
    catalog.create_table(id_staging, schema=staging_data.schema)
    catalog.load_table(id_staging).append(staging_data)

    try:
        catalog.rename_table(id_staging, id_target)
    except TableAlreadyExistsError as exc:
        q4_case = "conflict"
        q4_detail = f"TableAlreadyExistsError: {exc} — rename ĐÈ lên tên đã tồn tại bị TỪ CHỐI"
    except Exception as exc:  # câu hỏi D4 CHÍNH LÀ "hành vi thật là gì" — bắt rộng có chủ đích
        q4_case = "unexpected"
        q4_detail = f"{type(exc).__name__}: {exc} — KHÔNG phải TableAlreadyExistsError như đoán"
    else:
        try:
            after = catalog.load_table(id_target).scan().to_arrow()
        except Exception as exc:  # bắt rộng có chủ đích, xem D1 ở trên
            q4_case = "unexpected"
            q4_detail = (
                f"rename không lỗi nhưng đọc lại {id_target} sau đó lỗi: "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            after_ids = set(after.column("id").to_pylist())
            staging_ids = set(staging_data.column("id").to_pylist())
            if after_ids == staging_ids:
                q4_case = "replace_ok"
                q4_detail = (
                    f"rename_table({id_staging!r}, {id_target!r}) THÀNH CÔNG và THAY dữ liệu — "
                    f"{id_target} giờ có đúng {len(staging_ids)} id của staging, "
                    "không còn id của target cũ"
                )
            else:
                q4_case = "unexpected"
                matched = len(after_ids & staging_ids)
                q4_detail = (
                    f"rename không lỗi nhưng {id_target} không khớp đúng dữ liệu staging "
                    f"({matched}/{len(staging_ids)} id khớp) — hành vi cần điều tra thêm"
                )

    return ProbeDResult(
        q1_rename_ok=q1_rename_ok,
        q1_detail=q1_detail,
        q2_data_intact=q2_data_intact,
        q2_detail=q2_detail,
        q3_old_name_gone=q3_old_name_gone,
        q3_detail=q3_detail,
        q4_case=q4_case,
        q4_detail=q4_detail,
    )


def purge_s3_prefix(
    *, s3_endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str
) -> int:
    """Xoá THẲNG mọi object dưới `prefix` qua S3 API — xem "CẠM BẪY DỌN DẸP" ở
    docstring đầu file. Bản sao của cùng hàm trong `measure_ingest_pod.py`/
    `measure_write_path.py`: catalog (`drop_table`/`drop_namespace`/xoá
    warehouse) không chạm data file trên MinIO."""
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--snapshot-probe-rows",
        type=int,
        default=1_000,
        help="Số dòng/lô cho A và B — câu hỏi 'một hay nhiều snapshot' không phụ thuộc cỡ lô",
    )
    parser.add_argument(
        "--rows-per-batch",
        type=int,
        default=200_000,
        help="Số dòng/lô cho C — khớp mặc định measure_ingest_pod.py (Đo 1) để so RSS trực tiếp",
    )
    parser.add_argument(
        "--batches-c",
        type=int,
        default=20,
        help="Số lô giữ trong MỘT transaction ở phép đo C — khớp mặc định Đo 1",
    )
    parser.add_argument("--bucket", default="loom-local")
    parser.add_argument(
        "--lakekeeper-url",
        default="http://loom-lakekeeper:8181",
        help="DNS nội bộ cụm — xem mục 'Vì sao chạy TRONG CỤM' ở docstring",
    )
    parser.add_argument("--minio-endpoint", default="http://minio:9000")
    parser.add_argument("--namespace", default="bench_single_commit")
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
    warehouse_name = f"probe-single-commit-{uuid.uuid4().hex[:10]}"

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

    catalog = build_catalog(
        catalog_uri=catalog_uri, warehouse=warehouse_name, s3_endpoint=args.minio_endpoint
    )
    catalog.create_namespace_if_not_exists(args.namespace)

    id_a = f"{args.namespace}.probe_a"
    id_b = f"{args.namespace}.probe_b"
    id_c = f"{args.namespace}.probe_c"
    # D tự dựng bốn tên bảng của riêng nó (probe_d_src/dst/target/staging) bên
    # trong `probe_d()` — KHÔNG khai lại ở đây: `finally` bên dưới dọn dẹp
    # bằng cách liệt kê THẬT qua `catalog.list_tables`, không theo một danh
    # sách tên đã tạo (xem chú thích "CẠM BẪY MỚI" ở docstring đầu file — D1
    # rename đổi tên probe_d_src đi, nên một danh sách tên tĩnh sẽ trỏ vào một
    # tên không còn tồn tại).

    try:
        print(
            "=== GHI NHẬN LỊCH SỬ (A/B/C — câu hỏi đã trả lời, KHÔNG còn là cửa chặn) ===",
            flush=True,
        )
        print("=== A: hai append trong một transaction ===", flush=True)
        snapshots_a = probe_a(catalog, id_a, args.snapshot_probe_rows)

        print("=== B: overwrite + append trong một transaction ===", flush=True)
        snapshots_b = probe_b(catalog, id_b, args.snapshot_probe_rows)

        print(
            f"=== C: {args.batches_c} lô x {args.rows_per_batch:,} dòng trong một transaction ===",
            flush=True,
        )
        peaks_c = probe_c(catalog, id_c, args.rows_per_batch, args.batches_c)
        rss_final = peaks_c[-1]
        # Ngưỡng 15 MiB: dung sai nhiễu đo đạc bình thường (cấp phát bộ nhớ
        # nền của allocator, GC...). Mức leo THẬT đo được ở Đo 1 (406 - 284 =
        # 122 MiB qua 20 lô, KHÔNG có lô nào tụt) lớn hơn ngưỡng này hàng chục
        # lần — 15 MiB đủ thấp để không bỏ sót một xu hướng leo thật, đủ cao
        # để không báo "leo" vì nhiễu.
        growth_c = rss_final - peaks_c[0]
        pattern_c = "bò lên" if growth_c > 15.0 else "phẳng"

        print("", flush=True)
        print(f"A: hai append       -> {snapshots_a} snapshot", flush=True)
        print(f"B: overwrite+append -> {snapshots_b} snapshot", flush=True)
        print(
            f"C: RSS đỉnh với {args.batches_c} lô trong một transaction: "
            f"{rss_final:,.0f} MiB   ({pattern_c})",
            flush=True,
        )
        print(
            "(A/B/C không quyết định mã thoát nữa — xem D bên dưới. Số đo ở trên "
            "khớp 'KẾT QUẢ VÒNG A/B/C ĐÃ CHỐT' trong docstring nếu môi trường "
            "không đổi; nếu lệch, đọc lại docstring, KHÔNG coi đây là hồi quy.)",
            flush=True,
        )

        print("", flush=True)
        print(
            "=== CỬA CHẶN SỐNG (D — rename_table có dựng nổi chuỗi tráo bảng?) ===",
            flush=True,
        )
        result_d = probe_d(catalog, args.namespace, args.snapshot_probe_rows)
        print(
            f"D1 rename chạy được   : {result_d.q1_rename_ok}  ({result_d.q1_detail})", flush=True
        )
        print(
            f"D2 dữ liệu nguyên vẹn : {result_d.q2_data_intact}  ({result_d.q2_detail})",
            flush=True,
        )
        print(
            f"D3 tên cũ biến mất    : {result_d.q3_old_name_gone}  ({result_d.q3_detail})",
            flush=True,
        )
        print(f"D4 rename đè tên cũ   : {result_d.q4_case}  ({result_d.q4_detail})", flush=True)

        pass_ = result_d.q1_rename_ok and result_d.q2_data_intact and result_d.q3_old_name_gone

        print("", flush=True)
        if not pass_:
            print(
                "KHÔNG ĐẠT — rename_table KHÔNG dựng nổi một chuỗi tráo bảng đáng tin.",
                flush=True,
            )
            print(
                "            Không còn phương án dự phòng hiển nhiên. DỪNG và báo chủ dự án.",
                flush=True,
            )
        elif result_d.q4_case == "replace_ok":
            print(
                "ĐẠT — rename_table dựng được chuỗi tráo bảng. D4: rename ĐÈ THÀNH CÔNG.",
                flush=True,
            )
            print(
                "      Chuỗi tráo: MỘT bước rename(staging -> target) — NGUYÊN TỬ THẬT, "
                "target biến mất và được thay cùng lúc trong một lời gọi catalog.",
                flush=True,
            )
        else:
            print(
                "ĐẠT (có điều kiện) — rename_table dựng được chuỗi tráo bảng, "
                "NHƯNG D4: rename ĐÈ lên tên đã tồn tại KHÔNG được phép.",
                flush=True,
            )
            print(f"      ({result_d.q4_case}: {result_d.q4_detail})", flush=True)
            print(
                "      HỆ QUẢ (D3 + D4 cộng lại): rename là MOVE (D3 xác nhận tên cũ biến "
                "mất) NHƯNG TỪ CHỐI ghi đè lên một tên đã tồn tại (D4) — nên chuỗi tráo "
                "bảng của `full` BẮT BUỘC phải qua NHIỀU bước catalog, KHÔNG phải một: "
                "HOẶC drop(target) rồi rename(staging -> target), HOẶC ba bước "
                "rename(target -> target_old) + rename(staging -> target) + "
                "drop(target_old). Dù chọn chuỗi nào, có một CỬA SỔ mà tên target KHÔNG "
                "PHÂN GIẢI được (giữa hai/ba lời gọi catalog liên tiếp) — `full` chỉ GẦN "
                "nguyên tử, KHÔNG nguyên tử tuyệt đối, và spec KHÔNG được hứa một đảm bảo "
                "mà nó không có. Script này KHÔNG chọn thay chuỗi nào — báo chủ dự án để "
                "quyết định.",
                flush=True,
            )
        return 0 if pass_ else 1
    finally:
        # Dọn NGAY trong lần chạy này — Job sống một lần rồi chết, xem mục
        # "Dọn dẹp" ở docstring đầu file. HAI ĐIỂM SỬA SAU KHI CHẠY THẬT:
        #
        # (1) Xoá đúng những gì THẬT SỰ còn tồn tại trong namespace lúc này,
        #     KHÔNG xoá theo danh sách tên đã TẠO (id_a, id_b, ..., id_d_src):
        #     D1 đổi tên probe_d_src -> probe_d_dst, nên "xoá tên đã tạo" sẽ cố
        #     DROP một tên KHÔNG CÒN TỒN TẠI. Đã ăn đúng lỗi này thật:
        #     Lakekeeper trả 403 Forbidden cho trường hợp đó — thông báo
        #     "TableActionForbidden: Table not found or action can_drop
        #     forbidden for Anonymous" — KHÔNG phải 404, vì nó CỐ Ý không phân
        #     biệt "không tìm thấy" với "không được phép" cho một principal ẩn
        #     danh (tránh rò rỉ việc một bảng có tồn tại hay không cho người
        #     không có quyền xem nó). Hệ quả: PyIceberg ném `ForbiddenError`,
        #     KHÔNG phải `NoSuchTableError`, nên `contextlib.suppress
        #     (NoSuchTableError)` (bản cũ) không bắt được nó. Sửa bằng liệt kê
        #     THẬT qua `catalog.list_tables`, không phải thêm ForbiddenError
        #     vào danh sách bỏ qua — thêm một loại lỗi nữa vào suppress chỉ che
        #     triệu chứng, không sửa nguyên nhân (xoá theo tên ĐÃ TẠO thay vì
        #     tên ĐANG CÓ).
        #
        # (2) Dọn dẹp PHẢI best-effort và KHÔNG được che verdict: lần chạy vừa
        #     rồi D tính verdict ĐÚNG (in đủ bốn câu trả lời D1-D4) rồi vẫn
        #     thoát mã khác 0 vì bước dọn dẹp SAU ĐÓ ném lỗi chưa bắt — một
        #     false negative, thứ tệ hơn cả một phép đo không chạy: nó trông
        #     như hỏng trong khi câu trả lời đã đúng. `pass_` (dòng phía trên)
        #     đã CHỐT trước khi khối `finally` này chạy; mọi bước dọn dẹp dưới
        #     đây bắt lỗi RIÊNG, in CẢNH BÁO, và KHÔNG BAO GIỜ ném tiếp — nếu
        #     `finally` ném, Python đổi cả `return` phía trên thành một
        #     exception, xoá mất verdict đã tính đúng.
        cleanup_errors: list[str] = []

        try:
            existing_tables = [
                ".".join(identifier) for identifier in catalog.list_tables(args.namespace)
            ]
        except Exception as exc:  # bắt rộng có chủ đích — dọn dẹp không được ném, xem trên
            existing_tables = []
            cleanup_errors.append(f"list_tables({args.namespace}): {type(exc).__name__}: {exc}")

        for identifier in existing_tables:
            try:
                catalog.drop_table(identifier)
            except Exception as exc:  # bắt rộng có chủ đích, xem chú thích (1) ở trên
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
        try:
            deleted = purge_s3_prefix(
                s3_endpoint=args.minio_endpoint,
                access_key=access_key,
                secret_key=secret_key,
                bucket=args.bucket,
                prefix=key_prefix,
            )
        except Exception as exc:  # bắt rộng có chủ đích
            cleanup_errors.append(f"purge_s3_prefix({key_prefix}): {type(exc).__name__}: {exc}")

        print(
            f"đã dọn: {len(existing_tables)} bảng còn tồn tại (liệt kê thật lúc dọn, "
            f"không theo tên đã tạo) + namespace + warehouse, {deleted} object S3 "
            f"dưới {key_prefix}",
            flush=True,
        )
        for err in cleanup_errors:
            print(f"CẢNH BÁO DỌN DẸP (không đổi verdict/mã thoát ở trên): {err}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
