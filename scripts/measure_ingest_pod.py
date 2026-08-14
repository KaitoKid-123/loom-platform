"""Đo 1 của Giai đoạn 3a (CỬA CHẶN) — RAM của một lần ghi Iceberg THẬT TỪ TRONG CỤM.

## SỬA SAU REVIEW — bản đầu không ghi Iceberg

Bản đầu chỉ dựng một `pa.RecordBatch` trong RAM rồi `del` ngay — không có
`pyiceberg`, không catalog, không S3, không Parquet, không commit. Con số 235
MiB đo được là chi phí SINH dữ liệu Arrow, không phải chi phí GHI Iceberg
(Parquet row-group buffer, buffer của S3 client, sổ sách transaction/manifest
của PyIceberg đều KHÔNG nằm trong con số đó). Vì Đo 1 là CỬA CHẶN — mọi thứ sau
nó kế thừa sai số của nó — một con số đo nhầm đối tượng ở đây tệ hơn không đo.

Bản này ghi THẬT: bootstrap Lakekeeper, tạo warehouse/namespace/bảng, rồi mỗi
lô đi qua đúng `Lakehouse.create_from`/`Lakehouse.append` — con đường mà
connector thật của Giai đoạn 3a sẽ dùng (xem `packages/icebergkit/src/
loom_iceberg/lakehouse.py`: đây là API DUY NHẤT trong hệ thống biết Iceberg tồn
tại). Ghi Parquet + upload S3 + commit catalog đều nằm TRONG cửa sổ đo RSS.

## Vì sao KHÔNG cần `kubectl port-forward` như `scripts/measure_write_path.py`

Script đó chạy trên HOST nên phải giả lập một địa chỉ mà cả host lẫn pod
Lakekeeper (STS tự AssumeRole) cùng với tới được — xem "Cạm bẫy mạng" trong
docstring của nó. Script NÀY chạy THẲNG TRONG một pod của cụm, nên gọi DNS nội
bộ của Kubernetes là đủ: `http://minio:9000` và `http://loom-lakekeeper:8181`
(tên Service, xem `kubectl -n loom get svc`) — không có bước giả lập địa chỉ
nào cả. Đây là đường ĐƠN GIẢN HƠN đường host, không phải khó hơn — ghi rõ ở đây
để người sau không tự thêm lại port-forward mà đường này không cần.

## Credential MinIO

Pod cần credential GỐC của MinIO để Lakekeeper tự AssumeRole hộ lúc tạo
warehouse (xem `create_warehouse` trong `warehouse.py`) — client PyIceberg
không bao giờ thấy cặp này. `make measure-ingest-pod` tiêm chúng qua
`secretKeyRef` từ Secret `minio-root` (khoá `root-user`/`root-password`, cùng
Secret mà `make measure-write` đọc bằng `kubectl get secret` từ host) vào biến
môi trường `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`.

## Dọn dẹp — MỘT LẦN CHẠY, không cần `--cleanup` rời như bản host

`scripts/measure_write_path.py` tách `--cleanup` thành lệnh riêng vì nó phải
NỐI LẠI ĐƯỢC qua nhiều lần gọi (tiến trình có thể chạy nhiều giờ trên host,
Ctrl-C giữa chừng phải tiếp tục được). Job này sống một lần rồi chết trong vài
chục giây, nên dọn ngay trong CÙNG một lần chạy, ở khối `finally`: bỏ đăng ký
bảng + namespace, xoá warehouse qua management API, rồi xoá THẬT object trên S3
qua `purge_s3_prefix` — lặp lại đúng "CẠM BẪY DỌN DẸP" mà Giai đoạn 2c đã đo
trên Lakekeeper v0.9.2: `drop_table`/`drop_namespace`/xoá warehouse (204) xong,
`list_objects_v2` vẫn thấy nguyên data/metadata file dưới prefix của warehouse
đó — chỉ xoá thẳng qua S3 API mới thật sự giải phóng đĩa.

GIỚI HẠN CHẤP NHẬN ĐƯỢC: nếu tiến trình bị OOMKilled giữa chừng (chính là kết
quả cần biết nếu Đo 1 thất bại), `finally` không kịp chạy — warehouse/namespace/
bảng và object S3 của lần đó sẽ RÒ RỈ, phải dọn tay qua Lakekeeper management
API. Chấp nhận được cho một script đo một-lần: nếu Đo 1 báo BLOCKED, cả hướng
thiết kế phải đổi, và một warehouse rác vài trăm KB không đáng để đổi lấy một
cơ chế nối-lại phức tạp như bản host.

## Vẫn giữ nguyên phép kiểm PHẲNG

`resource.getrusage(RUSAGE_SELF).ru_maxrss` là đỉnh RSS tiến trình từng chạm
tới — vẫn là con số phải tin cho việc ĐẶT `limits.memory`, vì limit phải chặn
được ĐỈNH, không phải mức trung bình. (Nó KHÔNG phải là thứ mà bộ giết OOM của
cgroup v2 trực tiếp dùng để quyết định — cgroup v2 tự theo dõi độc lập bằng
`memory.current` so với `memory.max`, xem chú thích trong `Makefile` tại mục
tiêu `measure-ingest-pod`. Hai cơ chế khác nhau; `ru_maxrss` đáng tin vì nó
bắt được đỉnh TỨC THỜI mà một ảnh chụp `memory.current` tại một thời điểm có
thể bỏ lỡ — không phải vì cgroup đọc thẳng biến này.) KHÔNG dùng
`sys.getsizeof`/`tracemalloc`: PyArrow cấp bộ nhớ NGOÀI heap Python (buffer
Arrow là vùng nhớ C++), nên hai công cụ đó báo thiếu rất nhiều so với RSS thật.

Chạy TRONG pod, in RSS đỉnh ra stdout. `make measure-ingest-pod` dựng Job, chờ,
đọc log, rồi đo cả node.
"""

import argparse
import contextlib
import os
import sys
import uuid
from resource import RUSAGE_SELF, getrusage
from typing import TYPE_CHECKING

import boto3
import httpx
import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse

if TYPE_CHECKING:
    # `boto3-stubs` là dev dependency (xem `[dependency-groups]` ở pyproject.toml
    # gốc) — image `loom-query` build bằng `uv sync --frozen --no-dev` nên KHÔNG
    # có `mypy_boto3_s3` lúc chạy trong pod. Đã kiểm thật: import thẳng ở top
    # level ném `ModuleNotFoundError` trong container, dù chạy ổn trên host qua
    # `uv run` (venv dev đầy đủ) — đúng khác biệt mà `scripts/measure_write_path.py`
    # không gặp vì nó CHỈ chạy trên host. Đặt dưới TYPE_CHECKING để mypy (chạy
    # trên host) vẫn kiểm được kiểu, còn runtime trong pod thì bỏ qua khối này.
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef


def make_batch(rows: int, batch_index: int) -> pa.Table:
    """~268 byte/dòng, KHÁC NHAU từng dòng.

    32 ký tự hex x 8 lần lặp = 256 byte văn bản + 8 byte int64 của `id` + ~4
    byte overhead offset chuỗi của Arrow ≈ 268 byte/dòng (ĐÃ SỬA — bản trước
    ghi nhầm "~300 byte/dòng").

    Chuỗi lặp lại bị nén từ điển về gần 0 và biến bài đo thành bài đo của một bài
    toán khác — Giai đoạn 2 đã dính đúng bẫy đó một lần với `repeat('x', 512)`.
    Trả `pa.Table` (không phải `pa.RecordBatch`): `Lakehouse.append`/
    `create_from` nhận `pa.Table`.
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


def purge_s3_prefix(
    *, s3_endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str
) -> int:
    """Xoá THẲNG mọi object dưới `prefix` qua S3 API — cùng cách
    `scripts/measure_write_path.py` dùng, xem "CẠM BẪY DỌN DẸP" ở docstring
    đầu file: catalog (`drop_table`/`drop_namespace`/xoá warehouse) không chạm
    data file."""
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
    parser.add_argument("--rows-per-batch", type=int, default=200_000)
    parser.add_argument("--batches", type=int, default=20)
    parser.add_argument("--bucket", default="loom-local")
    parser.add_argument(
        "--lakekeeper-url",
        default="http://loom-lakekeeper:8181",
        help="DNS nội bộ cụm — xem mục 'Vì sao KHÔNG cần port-forward' ở docstring",
    )
    parser.add_argument("--minio-endpoint", default="http://minio:9000")
    parser.add_argument("--namespace", default="bench_ingest_pod")
    parser.add_argument("--table", default="measure_ingest_pod")
    args = parser.parse_args()

    # Credential GỐC MinIO — tiêm qua secretKeyRef trong Job spec, xem mục
    # "Credential MinIO" ở docstring đầu file. KeyError với traceback rõ ràng
    # hơn một lỗi 403 mù mờ từ Lakekeeper nếu ai quên mount Secret.
    access_key = os.environ["MINIO_ACCESS_KEY"]
    secret_key = os.environ["MINIO_SECRET_KEY"]

    management_url = args.lakekeeper_url
    catalog_uri = f"{management_url}/catalog"

    ensure_bootstrapped(management_url)

    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    key_prefix = prefix_for_lakehouse(workspace_id, lakehouse_id).rstrip("/")
    warehouse_name = f"measure-ingest-pod-{uuid.uuid4().hex[:10]}"

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
    house = Lakehouse(catalog)
    house.create_namespace_if_not_exists(args.namespace)
    qualified = f"{args.namespace}.{args.table}"

    total_rows = 0
    try:
        for i in range(args.batches):
            batch = make_batch(args.rows_per_batch, i)
            total_rows += batch.num_rows
            if i == 0:
                # Tạo bảng + ghi lô đầu trong MỘT lời gọi — khớp con đường mà
                # `mode: full` của connector thật sẽ dùng (đọc spec Giai đoạn
                # 3a mục 3.1).
                house.create_from(qualified, batch)
            else:
                house.append(qualified, batch)
            # Giữ ĐÚNG một lô sống tại một thời điểm. Nếu RSS vẫn bò lên theo số
            # lô thì chỗ rò không nằm trong vòng lặp Python — nó nằm trong
            # PyIceberg/PyArrow (buffer Parquet, state transaction tích luỹ...),
            # và đó chính là điều Đo 1 cần phát hiện.
            del batch
            peak = getrusage(RUSAGE_SELF).ru_maxrss / 1024
            print(
                f"[lô {i + 1:3d}/{args.batches}] dòng={total_rows:,} RSS đỉnh={peak:,.0f} MiB",
                flush=True,
            )

        peak = getrusage(RUSAGE_SELF).ru_maxrss / 1024
        print(f"KẾT QUẢ rss_peak_mib={peak:.0f} rows={total_rows}", flush=True)
        return 0
    finally:
        # Dọn NGAY trong lần chạy này — xem mục "Dọn dẹp" ở docstring đầu file
        # cho lý do vì sao khác với `--cleanup` rời của scripts/measure_write_path.py.
        #
        # Gọi THẲNG `catalog.drop_table`/`catalog.drop_namespace` (PyIceberg),
        # KHÔNG qua `house.drop_table`/`house.drop_namespace` (icebergkit) như
        # đoạn ghi ở trên: đã đo thật và vỡ thật — image `loom-query:dev` đang
        # chạy được build từ một bản `packages/icebergkit` CŨ hơn, chưa có hai
        # phương thức đó trên `Lakehouse` (`AttributeError` xác nhận bằng
        # `dir(Lakehouse)` chạy thật trong pod). `RestCatalog` của PyIceberg thì
        # có sẵn từ trước, không phụ thuộc bản icebergkit nào được bake vào
        # image — dùng thẳng nó ở bước dọn để không cần build lại image chỉ vì
        # một lần đo có thể phải bỏ.
        with contextlib.suppress(NoSuchTableError):
            catalog.drop_table(qualified)
        with contextlib.suppress(NoSuchNamespaceError):
            catalog.drop_namespace(args.namespace)
        with httpx.Client(timeout=30.0) as client:
            resp = client.delete(f"{management_url}/management/v1/warehouse/{warehouse_id}")
        if resp.status_code not in (204, 404):
            resp.raise_for_status()
        deleted = purge_s3_prefix(
            s3_endpoint=args.minio_endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=args.bucket,
            prefix=key_prefix,
        )
        print(
            f"đã dọn: bảng+namespace+warehouse, {deleted} object S3 dưới {key_prefix}", flush=True
        )


if __name__ == "__main__":
    sys.exit(main())
