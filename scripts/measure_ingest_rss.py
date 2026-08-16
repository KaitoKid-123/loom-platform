"""ĐO 5 của Giai đoạn 3d — RSS đỉnh của ĐƯỜNG NẠP THẬT, hai đường ghi, một lần chạy.

Đây là một PHÉP ĐO, không phải một tính năng. Nó không có người dùng, không có
đường vào từ API, và không có gì trong `services/` gọi tới nó.

## Câu hỏi, và vì sao nó quyết định việc TIẾP THEO có cần làm không

Backlog 3d nói việc "dựng Arrow theo CỘT" là BẮT BUỘC, với đúng một lý do: ĐO 3
đo lô 100.000 dòng ở **đỉnh RSS 587 MiB**, vượt `limits.memory` 512Mi của pod nạp,
nên cấu hình DUY NHẤT vượt được ngưỡng thông lượng lại là cấu hình bị OOMKill.

Nhưng con số 587 đó đo đường ghi CŨ — một `append` (tức một commit catalog) cho
MỖI lô. `scripts/probe_iceberg_add_files.py` đo đường `add_files` ở **173 MiB so
với 281 MiB** cho phần ghi, và `probe_read_path_cost.py` đo riêng đường ĐỌC ở
~451 MiB. Ba con số đó tới từ BA tiến trình khác nhau với ba cái nền khác nhau, và
cộng chúng trên giấy là đúng loại suy luận đã sai nhiều lần trong dự án này (ĐO 2
kết luận "PyIceberg không gộp được commit" từ một API; ngưỡng 14,7 MB/s chép từ
một phép đo không chạm mạng). Nên câu hỏi phải được ĐO, không được suy:

> Với `batch_rows=100.000`, đường nạp SAU khi có `add_files` đạt đỉnh RSS bao
> nhiêu, trong một pod THẬT, và nó có nằm dưới 512 MiB không?

Nếu CÓ thì lô 100k dòng hợp pháp mà không cần sửa đường đọc, và việc lớn nhất còn
lại của backlog không cần làm.

## Hai đường, HAI TIẾN TRÌNH CON — `ru_maxrss` không bao giờ giảm

Đo cả hai đường nối tiếp trong một tiến trình thì đường chạy SAU thừa hưởng đỉnh
của đường chạy TRƯỚC, nên nó KHÔNG THỂ đọc thấp hơn — tức là đúng kết quả mà phép
đo này tồn tại để tìm sẽ không bao giờ hiện ra. Mỗi đường vì vậy chạy trong một
`os.fork()` riêng, tự đọc `ru_maxrss` của CHÍNH NÓ rồi gửi về qua pipe. Hai con
được fork LIÊN TIẾP ở đầu lần chạy, trước khi cha kịp làm gì nặng, để hai cái nền
bằng nhau; và nền đó (`rss_at_fork_mib`) được IN RA cạnh đỉnh để người đọc trừ
được chứ không phải tin. Cùng cách và cùng lý do như `probe_iceberg_add_files.py`
mục "Vì sao Q2 phải fork".

`ru_maxrss` chứ không `memory.current` của cgroup: một trang page cache do đọc
Parquet/đọc socket sinh ra là bộ nhớ THU HỒI ĐƯỢC, và `memory.current` đếm nó —
xem `measure_ingest_pod.py` cho lần đã trả giá vì chuyện này.

## Hai đường ghi được dựng như thế nào, và vế nào là MÃ THẬT

Cả hai đường chạy CHÍNH `loom_task.runner.run_incremental` với CHÍNH
`PostgresConnector` — nên phần ĐỌC (lớp psycopg + `_rows_to_record_batch`, đúng
lớp mà backlog muốn viết lại) và phần vòng lặp là mã production, không phải một
bản mô phỏng. Khác biệt duy nhất là `Sink`:

    add_files   `loom_task.sink.IcebergSink` NGUYÊN BẢN + K nhóm (mã hôm nay)
    append      `_Phase3aSink` dưới đây: create_from ở lô đầu, `Lakehouse.append`
                mỗi lô sau, `commit()` rỗng, chạy với K = 1

`_Phase3aSink` là một BẢN DỰNG LẠI đường ghi 3a, không phải mã 3a lịch sử (mã đó
không còn tồn tại). Nó dựng lại đúng hai tính chất mà con số 587 phụ thuộc vào:
một commit catalog mỗi lô, và `pa.Table.from_batches` cho mỗi lô. Nói ra vì đó là
giới hạn của vế đối chứng: nếu 3a có một chi tiết tốn RAM nào KHÁC hai điều đó thì
vế `append` ở đây đo thấp hơn 3a thật.

Vế `add_files` KHÔNG có giới hạn đó — nó là mã sẽ chạy trong production.

## Cái phép đo này KHÔNG bao gồm, nói ra thay vì để người đọc giả định

* **Lời báo tiến độ qua HTTP.** Client ở đây giữ watermark trong RAM
  (`_LocalClient`), không gọi `/internal/ingest/*`. Một POST JSON vài trăm byte
  mỗi nhóm không phải một nguồn RSS đáng kể, nhưng nó CÓ mặt trong con số 587 của
  ĐO 3 — nên nếu hai phép đo lệch nhau vài MiB, đây là một trong các lý do.
* **`main.ingest`.** Không dựng `Settings`/`SourceCredentials` (chúng đòi biến môi
  trường của một Job do control plane phóng). `check_schema` cũng không chạy: nó
  là một phép so hai danh sách tên cột, không giữ dữ liệu.
* **Ngưỡng 512Mi không được THI HÀNH trong lần chạy này.** Job cố ý chạy với một
  `limits.memory` RỘNG: mục đích là ĐỌC được `ru_maxrss`, và một tiến trình bị
  OOMKill không báo được con số nào cả (đúng bẫy mà ĐO 1 đã ăn). Trần pod không
  làm RSS thấp đi — nó chỉ quyết định tiến trình có sống để kể lại hay không.

## Nguồn: Postgres TRONG CỤM, và KHÔNG một byte nào tới Aiven

Bảng nguồn là một bảng THẬT trong một Postgres dùng-một-lần chạy trong cụm (xem
`make bench-source-up`), bảy cột đúng hình dạng bảng bench của ĐO 3/ĐO 4
(`id bigint`, `event_time timestamptz`, `region text`, `status text`,
`amount float8`, `customer_id text`, `payload text` 220 ký tự ≈ 298 byte/dòng
Arrow). Không có đường nào trong file này chạm tới Aiven, và đó là một yêu cầu
tuyệt đối chứ không một lựa chọn: ĐO 3 đã seed 1,2 triệu dòng lên service Aiven
gói 1 GB đĩa của chủ dự án và lật CẢ SERVICE sang chỉ-đọc trong lúc control plane
đang sống.

Cái giá phải nói ra: đường dây tới nguồn giờ là mạng nội bộ cụm, không phải
internet. Điều đó làm THÔNG LƯỢNG ở đây không so được với ĐO 3 — và thông lượng
KHÔNG phải câu hỏi của phép đo này. RSS phía client thì so được, vì nó là số
object Python sống cùng lúc cho MỘT lô, và con số đó không biết gói tin đã đi bao
xa: cursor có tên vẫn `FETCH FORWARD batch_rows` một lần, psycopg vẫn dựng một
`dict` mỗi dòng, `_rows_to_record_batch` vẫn gom cùng ngần ấy dòng.

## Dọn dẹp

Bảng + namespace + warehouse + object S3, tất cả trong `finally`, best-effort,
từng bước một, KHÔNG BAO GIỜ ném tiếp: verdict đã chốt trước khi `finally` chạy,
và một lỗi dọn dẹp làm hỏng mã thoát của một phép đo đã tính đúng là một false
negative — tệ hơn cả không đo. `drop_table` KHÔNG xoá object trên S3 (đo ở Giai
đoạn 2c, xem `Lakehouse.drop_table`), nên prefix được xoá THẲNG qua S3 API.

S3 ở đây đi qua `pyarrow.fs.S3FileSystem` chứ không boto3: ảnh `loom/task` là ảnh
pod nạp THẬT và nó không có boto3 (xem `services/loom-task/pyproject.toml`) — dùng
một ảnh khác chỉ để dọn dẹp sẽ là đo trên một ảnh không phải ảnh production.

Chạy: `make bench-source-up` rồi `make measure-ingest-rss`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from resource import RUSAGE_SELF, getrusage
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
from pyarrow.fs import S3FileSystem  # type: ignore[import-untyped]

from loom_connector import StreamState
from loom_connector.postgres import PostgresConnector
from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse
from loom_task.runner import bronze_table_name, resolve_cursor, run_incremental
from loom_task.sink import IcebergSink

# Trần RAM của pod nạp — `task.memory` trong `deploy/helm/loom/values.yaml`. Viết
# ra để verdict nói được nó đang so với cái gì, và MiB chứ không Mi vì `ru_maxrss`
# đếm KiB (Linux) nên phép chia là 1024, không 1000.
_POD_MEMORY_LIMIT_MIB = 512


def rss_mib() -> float:
    """Đỉnh RSS của CHÍNH tiến trình này, MiB. Xem docstring đầu file."""
    return float(getrusage(RUSAGE_SELF).ru_maxrss) / 1024


class _LocalClient:
    """Đúng phần `IngestClientLike` mà `run_incremental` gọi, giữ watermark trong RAM.

    KHÔNG gọi `/internal/ingest/*` — xem mục "Cái phép đo này KHÔNG bao gồm".
    Watermark được giữ lại (chứ không bỏ đi) vì `run_incremental` đọc
    `current_state()` MỘT lần ở đầu, và một client trả về `StreamState()` rỗng là
    đúng hình dạng của lần nạp đầu tiên — cấu hình mà phép đo này muốn đo.
    """

    def __init__(self) -> None:
        self.reports = 0
        self.rows_reported = 0
        self.cursor_value: str | None = None

    @property
    def source_id(self) -> str:
        return "measure-ingest-rss"

    def current_state(self) -> StreamState:
        return StreamState()

    def report_progress(
        self,
        *,
        rows: int,
        cursor_column: str | None = None,
        cursor_type: str | None = None,
        cursor_value: str | None = None,
    ) -> None:
        self.reports += 1
        self.rows_reported += rows
        self.cursor_value = cursor_value

    def complete(self, *, status: str, error: str | None = None) -> None:
        raise AssertionError("phép đo không đóng run — không có run nào để đóng")


class _Phase3aSink:
    """Đường ghi của Giai đoạn 3a, dựng lại: MỘT commit catalog cho MỖI lô.

    Chỉ là vế ĐỐI CHỨNG. Xem mục "Hai đường ghi được dựng như thế nào" ở docstring
    đầu file cho điều nó dựng lại đúng và điều nó không bảo đảm.

    `commit()` rỗng vì ở 3a không có bước commit riêng — `append` đã commit. Chạy
    nó với `commit_every_batches=1` cho ra đúng nhịp của 3a: ghi-và-commit một lô,
    rồi báo watermark một lần cho lô đó.
    """

    def __init__(self, lakehouse: Lakehouse, *, target: str) -> None:
        self._lakehouse = lakehouse
        self._target = target
        self._created = False

    def write(self, batch: pa.RecordBatch) -> None:
        data = pa.Table.from_batches([batch])
        if not self._created:
            self._lakehouse.create_namespace_if_not_exists(self._target.rpartition(".")[0])
            self._created = True
            if not self._lakehouse.exists(self._target):
                self._lakehouse.create_from(self._target, data)
                return
        self._lakehouse.append(self._target, data)

    def commit(self) -> None:
        """3a không có bước này — `write` ở trên đã commit."""


@dataclass
class _SamplingSink:
    """Bọc một sink thật và ĐỌC `ru_maxrss` sau mỗi lời gọi.

    Có mặt vì một con số đỉnh duy nhất không phân biệt được hai hình dạng rất khác
    nhau: RSS PHẲNG sau lô đầu (chi phí là một lô sống trong RAM — hạ `batch_rows`
    là hạ được) và RSS LEO ĐỀU qua từng lô (có gì tích luỹ — hạ `batch_rows` không
    cứu, và một lần chạy dài hơn sẽ cao hơn con số đo được). ĐO 1 đã gặp đúng vế
    thứ hai (284 -> 406 MiB, không lô nào tụt), nên đây là dữ kiện phải thu, không
    phải một thứ trang trí.

    Bọc chứ không sửa `IcebergSink`: mã production không được mọc thêm một cái hook
    chỉ để một phép đo đọc được.
    """

    inner: Any
    after_write: list[float] = field(default_factory=list)
    after_commit: list[float] = field(default_factory=list)

    def write(self, batch: pa.RecordBatch) -> None:
        self.inner.write(batch)
        self.after_write.append(rss_mib())

    def commit(self) -> None:
        self.inner.commit()
        self.after_commit.append(rss_mib())


def _run_one_path(
    *,
    label: str,
    dsn: str,
    stream: str,
    batch_rows: int,
    commit_every: int,
    catalog_factory: Callable[[], Any],
    target: str,
    new_path: bool,
) -> dict[str, Any]:
    """Một lần nạp `incremental` đầy đủ qua MỘT trong hai đường ghi.

    `catalog_factory` chứ không một `RestCatalog` thừa hưởng từ cha: `RestCatalog`
    giữ một session HTTP với socket đang mở, và hai tiến trình cùng ghi lên một
    socket TCP thì hỏng theo cách rất khó đọc (cùng lý do đã ghi ở
    `probe_iceberg_add_files.py`).
    """
    lakehouse = Lakehouse(catalog_factory())
    inner: Any
    if new_path:
        inner = IcebergSink(lakehouse, target=target, run_id=uuid.uuid4(), stream=stream)
    else:
        inner = _Phase3aSink(lakehouse, target=target)
    sink = _SamplingSink(inner)

    connector = PostgresConnector(dsn=dsn, batch_rows=batch_rows)
    cursor = resolve_cursor(connector.discover(), stream, None)
    client = _LocalClient()

    started = time.perf_counter()
    # `type: ignore[arg-type]`: `_SamplingSink` khai ĐÚNG hai phương thức mà
    # `run_incremental` gọi (`write`, `commit`), còn Protocol `Sink` khai bảy — năm
    # cái kia thuộc đường `full`. Docstring của `Sink` đã nói ra chính sự lệch này
    # ("nhận một tham số khai 7 phương thức trong khi nó gọi 1") và giữ MỘT Protocol
    # có chủ đích. Tách thêm một Protocol hẹp chỉ để một script đo qua được mypy là
    # sửa mã sản phẩm cho phép đo, không phải ngược lại.
    rows = run_incremental(connector, sink, client, stream, cursor, commit_every)  # type: ignore[arg-type]
    seconds = time.perf_counter() - started

    return {
        "label": label,
        "rows": rows,
        "wall_seconds": seconds,
        "batches": len(sink.after_write),
        "commits": len(sink.after_commit),
        "watermark_reports": client.reports,
        "rows_reported": client.rows_reported,
        "rss_after_each_batch_mib": [round(value) for value in sink.after_write],
    }


def measure_isolated(label: str, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Chạy `work` trong một tiến trình CON và mang về `ru_maxrss` của chính nó.

    Con KHÔNG BAO GIỜ chạy tiếp mã của cha: `os._exit` bỏ qua mọi handler `atexit`,
    mọi khối `finally` đang chờ trong stack của cha, và — quan trọng nhất — khối
    dọn dẹp ở `main`. Một con thoát bằng `sys.exit` sẽ chạy khối dọn dẹp đó lần thứ
    hai, xoá warehouse ngay giữa lần chạy của cha.
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
        # thay vì để `json.loads("")` ném `JSONDecodeError`, một câu không gợi ý
        # gì về việc vừa có một tiến trình bị giết.
        return {
            "label": label,
            "error": f"tiến trình con {label!r} chết mà không gửi kết quả nào "
            "(nhiều khả năng bị giết: OOM hoặc tín hiệu)",
            "rss_at_fork_mib": at_fork,
        }
    result: dict[str, Any] = json.loads(raw)
    return result


def s3_filesystem(*, endpoint: str, access_key: str, secret_key: str) -> S3FileSystem:
    """`S3FileSystem` với credential GỐC MinIO — CHỈ để dọn dẹp.

    Tách scheme khỏi host thay vì đưa cả `http://minio:9000` vào
    `endpoint_override`: pyarrow nhận cả hai dạng, nhưng dạng tách rời không có
    trường hợp nào mơ hồ, và một endpoint đọc nhầm thành https cho ra một lỗi TLS
    chẳng nhắc gì tới cấu hình.
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=100_000,
        help="Số dòng mỗi lô ĐỌC. Mặc định 100.000 — CHÍNH cấu hình mà ĐO 3 đo ở "
        "587 MiB và bị OOMKill, tức là câu hỏi của phép đo này",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=5,
        help="K của đường add_files. Mặc định khớp `WriteTuning.commit_every_batches`",
    )
    parser.add_argument("--stream", default="bench.ingest_bench")
    parser.add_argument(
        "--paths",
        default="add_files,append",
        help="Đường ghi cần đo, phân cách bằng dấu phẩy. `append` là vế đối chứng "
        "(dựng lại đường 3a) — bỏ nó đi thì con số add_files không có gì để so",
    )
    parser.add_argument("--bucket", default="loom-local")
    parser.add_argument("--lakekeeper-url", default="http://loom-lakekeeper:8181")
    parser.add_argument("--minio-endpoint", default="http://minio:9000")
    args = parser.parse_args()

    # Credential GỐC MinIO — tiêm qua secretKeyRef (Secret `minio-root`), cùng
    # cách và cùng lý do như `measure_ingest_pod.py`: pod đọc thẳng Secret của cụm
    # nó đang chạy trong.
    access_key = os.environ["MINIO_ACCESS_KEY"]
    secret_key = os.environ["MINIO_SECRET_KEY"]
    # DSN nguồn ghép từ các mảnh trong Secret `bench-source` (xem
    # `make bench-source-up`). Ghép Ở ĐÂY chứ không truyền một DSN đủ: một chuỗi
    # có mật khẩu trong `env` của Job là một chuỗi hiện ra trong `kubectl describe`.
    source_host = os.environ.get("BENCH_SOURCE_HOST", "bench-pg")
    source_port = os.environ.get("BENCH_SOURCE_PORT", "5432")
    source_db = os.environ.get("BENCH_SOURCE_DBNAME", "bench")
    source_user = os.environ.get("BENCH_SOURCE_USER", "bench")
    source_password = os.environ["BENCH_SOURCE_PASSWORD"]
    dsn = f"postgresql://{source_user}:{source_password}@{source_host}:{source_port}/{source_db}"

    management_url = args.lakekeeper_url
    catalog_uri = f"{management_url}/catalog"
    ensure_bootstrapped(management_url)

    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    key_prefix = prefix_for_lakehouse(workspace_id, lakehouse_id).rstrip("/")
    warehouse_name = f"measure-ingest-rss-{uuid.uuid4().hex[:10]}"
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
    print(
        f"cấu hình: batch_rows={args.batch_rows:,}  K={args.commit_every}  "
        f"stream={args.stream}  nguồn={source_host}:{source_port}/{source_db}",
        flush=True,
    )

    def catalog_factory() -> Any:
        return build_catalog(
            catalog_uri=catalog_uri, warehouse=warehouse_name, s3_endpoint=args.minio_endpoint
        )

    wanted = [name.strip() for name in args.paths.split(",") if name.strip()]
    results: list[dict[str, Any]] = []
    try:
        # Hai con fork LIÊN TIẾP, trước khi cha kịp làm gì nặng — xem docstring.
        for name in wanted:
            new_path = name == "add_files"
            # Bảng RIÊNG cho mỗi đường: cùng một bảng thì đường chạy sau nối thêm
            # vào dữ liệu của đường trước và số dòng đọc lại được mất nghĩa. Slug
            # đi qua `bronze_table_name` để tên bảng theo đúng quy ước thật.
            target = bronze_table_name(f"measure-{name.replace('_', '-')}", args.stream)
            results.append(
                measure_isolated(
                    name,
                    lambda name=name, new_path=new_path, target=target: _run_one_path(  # type: ignore[misc]
                        label=name,
                        dsn=dsn,
                        stream=args.stream,
                        batch_rows=args.batch_rows,
                        commit_every=args.commit_every if new_path else 1,
                        catalog_factory=catalog_factory,
                        target=target,
                        new_path=new_path,
                    ),
                )
            )

        print("", flush=True)
        print("=== KẾT QUẢ ===", flush=True)
        for result in results:
            if "error" in result:
                print(f"{result.get('label', '?'):10s} HỎNG: {result['error']}", flush=True)
                continue
            print(
                f"{result['label']:10s} RSS đỉnh {result['rss_peak_mib']:7.1f} MiB "
                f"(nền lúc fork {result['rss_at_fork_mib']:.1f})  "
                f"{result['wall_seconds']:6.1f}s  "
                f"{result['rows']:,} dòng / {result['batches']} lô / "
                f"{result['commits']} commit / {result['watermark_reports']} lời báo",
                flush=True,
            )
            print(f"           RSS sau từng lô: {result['rss_after_each_batch_mib']}", flush=True)

        print("", flush=True)
        print("=== VERDICT ===", flush=True)
        new = next((r for r in results if r.get("label") == "add_files"), None)
        if new is None or "error" in new:
            print("KHÔNG KẾT LUẬN ĐƯỢC — đường add_files không chạy xong", flush=True)
            return 1
        peak = float(new["rss_peak_mib"])
        fits = peak < _POD_MEMORY_LIMIT_MIB
        print(
            f"batch_rows={args.batch_rows:,} trên đường add_files: {peak:.1f} MiB "
            f"{'<' if fits else '>='} trần pod {_POD_MEMORY_LIMIT_MIB} MiB -> "
            f"{'VỪA' if fits else 'KHÔNG VỪA'}",
            flush=True,
        )
        print(
            "Đọc con số này cùng dòng 'RSS sau từng lô': một đỉnh PHẲNG nghĩa là chi "
            "phí là MỘT lô sống trong RAM; một đỉnh còn LEO ở lô cuối nghĩa là con số "
            "trên là CẬN DƯỚI, và một lần chạy dài hơn sẽ cao hơn.",
            flush=True,
        )
        return 0
    finally:
        # Best-effort, từng bước một, KHÔNG BAO GIỜ ném tiếp — xem docstring.
        cleanup_errors: list[str] = []
        catalog = catalog_factory()
        namespaces = ["bronze"]
        for namespace in namespaces:
            try:
                existing = [".".join(identifier) for identifier in catalog.list_tables(namespace)]
            except Exception as exc:  # bắt rộng có chủ đích — dọn dẹp không được ném
                existing = []
                cleanup_errors.append(f"list_tables({namespace}): {type(exc).__name__}: {exc}")
            for identifier in existing:
                try:
                    catalog.drop_table(identifier)
                except Exception as exc:  # bắt rộng có chủ đích
                    cleanup_errors.append(f"drop_table({identifier}): {type(exc).__name__}: {exc}")
            try:
                catalog.drop_namespace(namespace)
            except Exception as exc:  # bắt rộng có chủ đích
                cleanup_errors.append(f"drop_namespace({namespace}): {type(exc).__name__}: {exc}")

        try:
            import httpx

            with httpx.Client(timeout=30.0) as client:
                resp = client.delete(f"{management_url}/management/v1/warehouse/{warehouse_id}")
            if resp.status_code not in (204, 404):
                resp.raise_for_status()
        except Exception as exc:  # bắt rộng có chủ đích
            cleanup_errors.append(f"delete warehouse {warehouse_id}: {type(exc).__name__}: {exc}")

        try:
            fs = s3_filesystem(
                endpoint=args.minio_endpoint, access_key=access_key, secret_key=secret_key
            )
            fs.delete_dir_contents(f"{args.bucket}/{key_prefix}", missing_dir_ok=True)
        except Exception as exc:  # bắt rộng có chủ đích
            cleanup_errors.append(f"purge {key_prefix}: {type(exc).__name__}: {exc}")

        print("", flush=True)
        print(f"đã dọn: bảng + namespace + warehouse + object S3 dưới {key_prefix}", flush=True)
        for err in cleanup_errors:
            print(f"CẢNH BÁO DỌN DẸP (không đổi verdict/mã thoát ở trên): {err}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
