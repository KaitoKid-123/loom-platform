"""Phép đo rủi ro #4 (CỬA CHẶN Giai đoạn 2) — đường GHI của PyIceberg, tách theo GIAI ĐOẠN.

Câu hỏi: PyIceberg có đủ nhanh cho đường ghi 50 GB, hay phải cắm `TrinoEngine`
qua interface `ComputeEngine` (biện pháp đã ghi sẵn trong spec v1)? Ngưỡng
ĐÃ CHỐT trước khi đo, không đổi sau khi thấy số:

    < 60 phút            ĐẠT — giữ PyIceberg
    60 - 180 phút        giữ cho Giai đoạn 3, nhưng đưa TrinoEngine vào lộ trình có ngày
    > 180 phút, hết bộ nhớ, hoặc commit hỏng   cắm Trino ở Giai đoạn 3

Chạy nhỏ (kiểm — đây là phạm vi việc VIẾT script này):

    make measure-write ARGS="--target-raw-gb 1 --batch-raw-mb 100"

Chạy 50 GB đầy đủ (NỀN — KHÔNG chạy trong lúc viết/kiểm script này, mất nhiều giờ):

    nohup make measure-write > /tmp/measure-write-50gb.out 2>&1 &

Dọn sạch một lần chạy (bảng + namespace + warehouse + DATA THẬT trên S3 — xem
"CẠM BẪY DỌN DẸP" bên dưới):

    make measure-write-cleanup

## Môi trường: cụm k3d qua MinIO LOCAL, không phải testcontainer

Câu hỏi của phép đo là "PyIceberg có đủ nhanh không", không phải "mạng có đủ
nhanh không". Đo qua một đường mạng chưa dựng (VD: MinIO trên VPS) sẽ trộn hai
câu hỏi làm một. Cụm local (MinIO chạy trong k3d, đĩa THẬT chứ không phải
testcontainer trên cùng máy) cho CẬN TRÊN của thời gian PyIceberg cần — bất cứ
đường nào chậm hơn (VD: MinIO ở xa) chỉ có thể chậm HƠN con số này, không thể
nhanh hơn.

## Cạm bẫy mạng — vì sao có hai `kubectl port-forward` với hai địa chỉ khác nhau

Lakekeeper chạy `sts-enabled=true`: nó CẤP LẠI cho client đúng `endpoint` ghi
trong storage-profile của warehouse, đè lên bất cứ giá trị nào client tự đặt
(xem `packages/icebergkit/src/loom_iceberg/catalog.py` — cùng cạm bẫy, đã ghi
ở đó cho testcontainers). Địa chỉ đó phải là một địa chỉ mà CẢ HAI phía với tới
được:

  - script này, chạy trên HOST (để ghi Parquet thật lên MinIO)
  - pod Lakekeeper, chạy TRONG cụm (để tự AssumeRole hộ, lấy credential STS)

`kubectl port-forward` mặc định bind `127.0.0.1` — chỉ HOST với tới được,
KHÔNG giúp gì cho phía pod. Địa chỉ dùng được cho cả hai, đã KIỂM THẬT trên cụm
này trước khi viết script (không suy đoán): địa chỉ GATEWAY của mạng docker mà
k3d dựng cho cụm (`k3d-<tên-cụm>`, mặc định `172.20.0.1` trên máy này) — HOST
với tới được vì đó là interface bridge CỦA CHÍNH NÓ; pod với tới được vì mọi
gói tin egress của nó đi qua NAT của node, và node thoát ra đúng qua gateway
đó. Đây là bản k3d của đúng thủ thuật docker-bridge-gateway mà
`packages/icebergkit/tests/integration/conftest.py` dùng cho testcontainers —
chỉ khác lớp mạng (k3d lồng một lớp CNI bên trong container node).

`--address <gateway>` cho `kubectl port-forward svc/minio` bind đúng lên địa
chỉ đó. Catalog (Lakekeeper) thì KHÔNG cần trò này — chỉ HOST gọi REST catalog,
Lakekeeper không cần gọi ngược lại — nên port-forward của nó bind `127.0.0.1`
bình thường.

## CẠM BẪY DỌN DẸP — `drop_table`/`drop_namespace`/xoá warehouse KHÔNG xoá đĩa

Đã kiểm thật trên Lakekeeper v0.9.2: `drop_table` + `drop_namespace` + xoá cả
warehouse qua `DELETE /management/v1/warehouse/{id}` (204) xong, `list_objects_v2`
trên MinIO vẫn thấy NGUYÊN data file + metadata file dưới prefix của warehouse
đó. `delete-profile: hard` trong storage-profile KHÔNG có nghĩa Lakekeeper tự
xoá hộ lúc DROP — nó chỉ là chính sách cho các thao tác xoá riêng của nó, và
qua thực nghiệm không kích hoạt ở đường này. `_purge_s3_prefix` bên dưới xoá
THẲNG qua S3 API — đây là bước DUY NHẤT thật sự giải phóng đĩa, và bỏ qua nó
nghĩa là môt bài đo 50 GB để lại 50 GB rác trên đĩa máy chủ vĩnh viễn.

## Bốn giai đoạn, đo RIÊNG — lý do cả script này tồn tại

  1. Sinh/đọc nguồn   — dựng bảng Arrow trong RAM (`make_batch`)
  2. Biến đổi         — KHÔNG có bước này trong phép đo. Ghi thật 0.0s mỗi lô,
                        không phải một placeholder: benchmark này canh đường
                        NGUỒN → ICEBERG thẳng, không có một pipeline biến đổi ở
                        giữa để đo. Nếu Giai đoạn 3 thêm biến đổi thật, chèn nó
                        vào giữa `make_batch` và `append_batch_timed` bên dưới
                        — ĐỪNG bịa một con số ở đây để "cho đủ bốn cột".
  3. Ghi Iceberg      — ghi data file Parquet thẳng client→S3 (PyArrowFileIO,
                        `sts-enabled=true` nên KHÔNG qua Lakekeeper)
  4. Commit catalog   — một PUT REST duy nhất tới Lakekeeper mỗi lô

Tách 3 và 4 đòi đi THẲNG vào PyIceberg (`Table.transaction()`), không qua
`Lakehouse.append()` — xem docstring của `append_batch_timed`.

## "50 GB" nghĩa là gì

`--target-raw-gb` là byte Arrow THÔ trong RAM lúc sinh dữ liệu (GB thập phân,
1e9 byte) — đây là đại lượng script ĐIỀU KHIỂN được (số dòng nhân kích thước
mỗi dòng, đo qua `pa.Table.nbytes`). Số này KHÔNG phải số byte cuối cùng nằm trên
đĩa: Parquet nén, tỉ lệ tuỳ dữ liệu. Cuối lần chạy, script ĐO THÊM byte Parquet
NÉN thật sự nằm trên S3 (`Lakehouse.scan_size_bytes`, đọc thẳng thống kê
manifest, không mở data file) và in cả hai — vì đó là thứ người dùng cảm nhận
được, còn byte thô là thứ đảm bảo bài đo không lặp lại bẫy Giai đoạn 2a (một
cột `repeat('x', N)` giống hệt nhau nén gần về 0, và bài đo "chạy tốt" mà
chẳng chạm bộ nhớ/băng thông nào thật).

## Dữ liệu: biến thiên THẬT, hình dạng nghiệp vụ

`id`/`event_time` tăng dần (vector hoá qua `pyarrow.compute`, không lặp
Python). `region` lực lượng THẤP (16 giá trị, vector hoá qua `take`).
`status` lực lượng thấp nhưng ngẫu nhiên thật. `amount` số thực ngẫu nhiên
thật. `customer_id` lực lượng CAO (64 bit ngẫu nhiên/dòng — 2^64 khả năng,
không dòng nào trùng dòng nào trong một lần chạy thật). `payload` là cột đệm
để đạt khối lượng byte mục tiêu, ngẫu nhiên THẬT theo từng dòng — ĐÂY LÀ CHỖ
Giai đoạn 2a dính bẫy (`repeat('x', 512)` giống hệt nhau, nén từ điển teo gần
về 0). Bốn cột cuối đòi ngẫu nhiên thật/dòng nên PHẢI lặp mức Python — không
có nguồn ngẫu nhiên vector hoá nào trong workspace này mà không kéo thêm
numpy (chưa phải dependency).

## Kiểm đĩa trống TRƯỚC — hàng rào duy nhất chặn lấp đầy đĩa máy chủ

Đo được ở Giai đoạn 2c Task 1: `local-path` KHÔNG thi hành hạn mức PVC — ghi
21 GB vào PVC khai "20Gi" chạy trơn. Nghĩa là không có gì ở TẦNG CỤM ngăn bài
đo này lấp đầy đĩa của CẢ MÁY, không chỉ của cụm (dữ liệu PVC của MinIO nằm
thẳng trên `/` của host qua `local-path`, đã xác nhận `/var/lib/docker` và `/`
cùng một filesystem trên máy dùng để viết script này). `check_disk_space` bên
dưới từ chối chạy nếu không đủ.

## Nối lại được

Mỗi lô là một `append` ĐỘC LẬP. Tiến trình ghi ra `--state-dir/progress.json`
(atomic write: `.tmp` rồi `rename`) SAU MỖI LÔ — đứt giữa chừng (Ctrl-C, mất
điện, OOM) chạy lại ĐÚNG LỆNH CŨ sẽ tự tiếp tục từ lô kế tiếp lô đã ghi nhận.
`--start-batch N` ép bỏ qua tới lô N bất kể file tiến trình nói gì — dùng để
kiểm nối lại, hoặc khi biết chắc N lô đầu đã commit xong trên Lakekeeper dù
file tiến trình cục bộ bị mất. CẢNH BÁO: dùng `--start-batch` để LÙI lại một lô
ĐÃ ghi nhận sẽ ghi dữ liệu TRÙNG — Iceberg chỉ APPEND, không có khái niệm "ghi
đè lô N".
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import math
import random
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import httpx
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse

REPO_ROOT = Path(__file__).resolve().parents[1]

# ─────────────────────────── dữ liệu mẫu ────────────────────────────

_BASE_EPOCH_US = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1_000_000)
_US_PER_ROW = 1_000_000  # mỗi dòng cách dòng trước đúng 1 giây trên cột event_time

REGIONS = [f"region-{i:02d}" for i in range(16)]  # lực lượng THẤP
STATUSES = ["pending", "processing", "completed", "failed", "refunded"]  # lực lượng thấp

# `pyarrow.compute` KHÔNG có `mod`/`modulo` (đã kiểm: chỉ có `divide`, không có
# phép chia lấy dư nào cho mảng). 16 là luỹ thừa của 2 nên `id & 15 == id % 16`
# — dùng `bit_wise_and` để vector hoá thay vì lặp Python. Assert dưới đây canh
# giả định đó nếu ai đổi số lượng REGIONS sau này.
assert len(REGIONS) > 0 and (len(REGIONS) & (len(REGIONS) - 1)) == 0, (
    "len(REGIONS) phải là luỹ thừa của 2 để _REGION_MASK thay được cho mod"
)
_REGION_MASK = len(REGIONS) - 1

# ~220 byte văn bản/dòng cho cột đệm — đủ để một bảng "giống nghiệp vụ" (id +
# thời gian + vài cột định danh + một cột mô tả) mà không cần quá nhiều dòng để
# chạm khối lượng mục tiêu. PHẢI ngẫu nhiên thật/dòng — xem docstring đầu file.
_PAYLOAD_HEX_CHARS = 220
_PAYLOAD_BITS = _PAYLOAD_HEX_CHARS * 4


def make_batch(*, offset: int, rows: int, seed: int) -> pa.Table:
    """Dựng một lô Arrow `rows` dòng, id bắt đầu từ `offset`.

    `id`/`event_time`/`region` vector hoá hoàn toàn qua `pyarrow.compute` —
    không có vòng lặp Python nào cho ba cột này, dù `rows` là hàng triệu.
    `status`/`amount`/`customer_id`/`payload` cần ngẫu nhiên THẬT theo từng
    dòng nên lặp mức Python — đây là chi phí thật của việc SINH dữ liệu, được
    đo riêng (giai đoạn 1), không giấu vào giai đoạn nào khác.
    """
    ids = pa.array(range(offset, offset + rows), type=pa.int64())

    micros = pc.add(
        pc.multiply(ids, pa.scalar(_US_PER_ROW, type=pa.int64())),
        pa.scalar(_BASE_EPOCH_US, type=pa.int64()),
    )
    event_time = micros.cast(pa.timestamp("us"))

    region_idx = pc.bit_wise_and(ids, pa.scalar(_REGION_MASK, type=pa.int64()))
    region = pc.take(pa.array(REGIONS, type=pa.string()), region_idx)

    rng = random.Random(seed)  # noqa: S311 - dữ liệu đo, không phải mật mã
    n_statuses = len(STATUSES)
    statuses = [STATUSES[rng.randrange(n_statuses)] for _ in range(rows)]
    amounts = [rng.uniform(0.01, 25_000.0) for _ in range(rows)]
    # 64 bit ngẫu nhiên/dòng: lực lượng CAO, không đụng hàng trong một lần chạy.
    customer_ids = [f"cust-{rng.getrandbits(64):016x}" for _ in range(rows)]
    # Cột đệm — PHẢI khác nhau từng dòng, xem docstring đầu file (bẫy GĐ 2a).
    payloads = [f"{rng.getrandbits(_PAYLOAD_BITS):0{_PAYLOAD_HEX_CHARS}x}" for _ in range(rows)]

    return pa.table(
        {
            "id": ids,
            "event_time": event_time,
            "region": region,
            "status": pa.array(statuses, type=pa.string()),
            "amount": pa.array(amounts, type=pa.float64()),
            "customer_id": pa.array(customer_ids, type=pa.string()),
            "payload": pa.array(payloads, type=pa.string()),
        }
    )


# ─────────────────────────── ghi tách giai đoạn ────────────────────────────


def append_batch_timed(
    catalog: RestCatalog, identifier: str, data: pa.Table
) -> tuple[float, float]:
    """Ghi MỘT lô, trả `(giây_ghi_file, giây_commit_catalog)` TÁCH RIÊNG.

    `Lakehouse.append()` gói cả hai bước làm MỘT lời gọi — đúng cho production
    (xem docstring của nó), nhưng phép đo này cần tách chúng, nên đi THẲNG vào
    PyIceberg thay vì qua `Lakehouse`. Đã đọc mã nguồn `pyiceberg/table/
    __init__.py` (0.11.x, bản cài trong workspace này) để xác nhận, KHÔNG suy
    đoán: `Table.append(df)` nội bộ làm

        with self.transaction() as tx:
            tx.append(df)

    `tx.append(df)` gọi `_dataframe_to_data_files(...)` — GHI data file Parquet
    thẳng lên S3 qua `PyArrowFileIO` (vì warehouse chạy `sts-enabled=true`,
    client tự cầm credential STS, không qua Lakekeeper cho bước này) — và chỉ
    XẾP một `AppendFiles` update vào bộ nhớ của transaction, KHÔNG gửi request
    nào. `Transaction.__exit__` (hoặc gọi tay `commit_transaction()`) mới gửi
    MỘT request PUT duy nhất (`Table._do_commit`) tới Lakekeeper. Gọi tay từng
    bước dưới đây tách đúng ranh giới đó.

    `catalog.load_table(identifier)` (một GET metadata) TÍNH VÀO thời gian
    commit: đó là một round trip REST tới Lakekeeper (không phải I/O dữ liệu),
    và là điều kiện bắt buộc để build một request commit hợp lệ (optimistic
    concurrency cần biết snapshot hiện tại).
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


# ─────────────────────────── trạng thái / nối lại ────────────────────────────


@dataclass
class SetupTimes:
    """Chi phí MỘT LẦN của một lần chạy MỚI — KHÔNG cộng vào bốn giai đoạn
    trên mỗi lô, vì nó không co giãn theo kích thước bảng."""

    ensure_bootstrapped_s: float
    create_warehouse_s: float
    create_namespace_s: float
    create_table_s: float
    calibration_generate_s: float


@dataclass
class BatchRecord:
    index: int
    rows: int
    raw_bytes: int
    generate_s: float
    transform_s: float
    iceberg_write_s: float
    catalog_commit_s: float
    completed_at: str


@dataclass
class Progress:
    bucket: str
    key_prefix: str
    workspace_id: str
    lakehouse_id: str
    warehouse_name: str
    warehouse_id: str
    iceberg_namespace: str
    table_name: str
    target_raw_gb: float
    batch_raw_mb: float
    bytes_per_row: float
    rows_per_batch: int
    total_batches_planned: int
    seed: int
    setup: SetupTimes
    batches: list[BatchRecord] = field(default_factory=list)
    # Cộng dồn qua NHIỀU lần gọi (nối lại sau khi đứt) — KHÔNG phải thời gian
    # của riêng lần gọi hiện tại. Thiếu trường này, tổng kết sau một lần nối
    # lại chỉ đếm thời gian của lần gọi CUỐI, nói dối về tổng thời gian thật.
    elapsed_s: float = 0.0


def save_progress(path: Path, progress: Progress) -> None:
    """Ghi ATOMIC: `.tmp` rồi `rename` — đứt giữa chừng lúc ghi không làm
    hỏng file tiến trình mà lần chạy sau cần đọc để nối lại."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(progress), indent=2, ensure_ascii=False))
    tmp.replace(path)


def load_progress(path: Path) -> Progress:
    raw: dict[str, Any] = json.loads(path.read_text())
    setup_raw: dict[str, Any] = raw["setup"]
    batches_raw: list[dict[str, Any]] = raw["batches"]
    return Progress(
        bucket=raw["bucket"],
        key_prefix=raw["key_prefix"],
        workspace_id=raw["workspace_id"],
        lakehouse_id=raw["lakehouse_id"],
        warehouse_name=raw["warehouse_name"],
        warehouse_id=raw["warehouse_id"],
        iceberg_namespace=raw["iceberg_namespace"],
        table_name=raw["table_name"],
        target_raw_gb=raw["target_raw_gb"],
        batch_raw_mb=raw["batch_raw_mb"],
        bytes_per_row=raw["bytes_per_row"],
        rows_per_batch=raw["rows_per_batch"],
        total_batches_planned=raw["total_batches_planned"],
        seed=raw["seed"],
        setup=SetupTimes(**setup_raw),
        batches=[BatchRecord(**b) for b in batches_raw],
        elapsed_s=raw.get("elapsed_s", 0.0),
    )


# ─────────────────────────── hạ tầng: đĩa, cụm, mạng ────────────────────────────


class Logger:
    """In ra stdout VÀ ghi file — để `tail -f` theo dõi được một lần chạy
    nhiều giờ, đúng yêu cầu "quan sát được trong lúc chạy"."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")

    def line(self, msg: str) -> None:
        print(msg)
        self._fh.write(msg + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def check_disk_space(path: Path, required_gb: float, log: Logger) -> None:
    """TỪ CHỐI chạy nếu không đủ chỗ — hàng rào DUY NHẤT chặn bài đo lấp đầy
    đĩa của cả máy (xem docstring đầu file). Kiểm trên `path` (mặc định `/`
    của HOST, không phải trong pod): dữ liệu PVC của MinIO qua `local-path`
    nằm thẳng trên đĩa host, không có hạn mức nào ở tầng cụm thi hành."""
    free_gb = shutil.disk_usage(path).free / 1_000_000_000
    if free_gb < required_gb:
        log.line(
            f"TỪ CHỐI CHẠY: cần tối thiểu {required_gb:.1f} GB trống tại '{path}', "
            f"chỉ đo được {free_gb:.1f} GB."
        )
        log.line(
            "Đây là hàng rào DUY NHẤT chặn bài đo lấp đầy đĩa của cả máy — "
            "xem docstring đầu file. Giảm --target-raw-gb, dọn đĩa, hoặc chỉ "
            "--check-path sang một filesystem khác còn chỗ."
        )
        raise SystemExit(1)
    log.line(f"Đĩa trống tại '{path}': {free_gb:.1f} GB — đủ cho yêu cầu {required_gb:.1f} GB.")


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    return result.stdout.strip()


def current_context() -> str:
    try:
        return _run(["kubectl", "config", "current-context"])
    except subprocess.CalledProcessError:
        return "none"


def require_context(cluster_name: str, log: Logger) -> None:
    want = f"k3d-{cluster_name}"
    ctx = current_context()
    if ctx != want:
        log.line(f"Context hiện tại: '{ctx}' — không phải '{want}'.")
        log.line(f"Chạy 'make cluster-up' hoặc 'kubectl config use-context {want}' trước.")
        raise SystemExit(1)


def bridge_gateway_ip(network: str) -> str:
    """IP gateway của mạng docker mà k3d dựng cho cụm — xem "Cạm bẫy mạng"
    ở docstring đầu file. Cùng lệnh `docker network inspect` mà
    `packages/icebergkit/tests/integration/conftest.py` dùng cho bridge mặc
    định, ở đây trỏ vào mạng riêng của k3d (`k3d-<tên-cụm>`)."""
    fmt = "{{(index .IPAM.Config 0).Gateway}}"
    return _run(["docker", "network", "inspect", network, "-f", fmt])


def kubectl_secret(k8s_namespace: str, name: str, key: str) -> str:
    encoded = _run(
        ["kubectl", "-n", k8s_namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"]
    )
    return base64.b64decode(encoded).decode()


def _wait_for_tcp(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)
    raise TimeoutError(f"{host}:{port} không mở sau {timeout}s ({last_error})")


@contextmanager
def port_forward(
    *, k8s_namespace: str, service: str, local_port: int, remote_port: int, address: str
) -> Iterator[None]:
    """`kubectl port-forward` chạy nền, dọn theo `finally` kể cả khi khối
    `with` ném lỗi — không để lại một tiến trình kubectl mồ côi sau một lần
    chạy hỏng giữa chừng."""
    proc = subprocess.Popen(  # noqa: S603
        [  # noqa: S607
            "kubectl",
            "-n",
            k8s_namespace,
            "port-forward",
            "--address",
            address,
            f"svc/{service}",
            f"{local_port}:{remote_port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_tcp(address, local_port)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def purge_s3_prefix(
    *, s3_endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str
) -> int:
    """Xoá THẲNG mọi object dưới `prefix` qua S3 API — bước DUY NHẤT thật sự
    giải phóng đĩa. Xem "CẠM BẪY DỌN DẸP" ở docstring đầu file: catalog
    (`drop_table`/`drop_namespace`/xoá warehouse) không chạm data file."""
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


def _format_eta(seconds: float) -> str:
    if math.isinf(seconds) or seconds != seconds:  # NaN kiểm bằng seconds != seconds
        return "?"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ─────────────────────────── thiết lập lần chạy mới ────────────────────────────


def fresh_setup(
    *,
    management_url: str,
    catalog_uri: str,
    s3_endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    iceberg_namespace: str,
    table_name: str,
    target_raw_gb: float,
    batch_raw_mb: float,
    calibration_rows: int,
    seed: int,
    log: Logger,
) -> Progress:
    t0 = time.perf_counter()
    ensure_bootstrapped(management_url)
    bootstrap_s = time.perf_counter() - t0

    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    key_prefix = prefix_for_lakehouse(workspace_id, lakehouse_id).rstrip("/")
    warehouse_name = f"bench-write-{uuid.uuid4().hex[:10]}"

    t0 = time.perf_counter()
    warehouse_id = create_warehouse(
        management_url,
        name=warehouse_name,
        bucket=bucket,
        key_prefix=key_prefix,
        s3_endpoint=s3_endpoint,
        access_key=access_key,
        secret_key=secret_key,
    )
    warehouse_s = time.perf_counter() - t0
    log.line(f"Warehouse mới: {warehouse_name} ({warehouse_id})  prefix={key_prefix}")

    catalog = build_catalog(
        catalog_uri=catalog_uri, warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )

    t0 = time.perf_counter()
    catalog.create_namespace_if_not_exists(iceberg_namespace)
    namespace_s = time.perf_counter() - t0

    log.line(f"Hiệu chỉnh kích thước dòng qua {calibration_rows:,} dòng mẫu...")
    t0 = time.perf_counter()
    sample = make_batch(offset=0, rows=calibration_rows, seed=seed)
    calib_generate_s = time.perf_counter() - t0
    bytes_per_row = sample.nbytes / sample.num_rows

    identifier = f"{iceberg_namespace}.{table_name}"
    t0 = time.perf_counter()
    catalog.create_table(identifier, schema=sample.schema)
    create_table_s = time.perf_counter() - t0

    target_bytes = target_raw_gb * 1_000_000_000
    batch_bytes = batch_raw_mb * 1_000_000
    rows_per_batch = max(1, round(batch_bytes / bytes_per_row))
    total_batches_planned = max(1, math.ceil(target_bytes / batch_bytes))

    log.line(
        f"Hiệu chỉnh: {bytes_per_row:.1f} byte/dòng ({calib_generate_s:.2f}s sinh mẫu) "
        f"=> {rows_per_batch:,} dòng/lô, {total_batches_planned} lô cho mục tiêu "
        f"{target_raw_gb:.3f} GB thô"
    )

    return Progress(
        bucket=bucket,
        key_prefix=key_prefix,
        workspace_id=str(workspace_id),
        lakehouse_id=str(lakehouse_id),
        warehouse_name=warehouse_name,
        warehouse_id=warehouse_id,
        iceberg_namespace=iceberg_namespace,
        table_name=table_name,
        target_raw_gb=target_raw_gb,
        batch_raw_mb=batch_raw_mb,
        bytes_per_row=bytes_per_row,
        rows_per_batch=rows_per_batch,
        total_batches_planned=total_batches_planned,
        seed=seed,
        setup=SetupTimes(
            ensure_bootstrapped_s=bootstrap_s,
            create_warehouse_s=warehouse_s,
            create_namespace_s=namespace_s,
            create_table_s=create_table_s,
            calibration_generate_s=calib_generate_s,
        ),
        batches=[],
    )


# ─────────────────────────── vòng lặp chính ────────────────────────────


def run_batches(
    progress: Progress,
    progress_path: Path,
    *,
    catalog_uri: str,
    s3_endpoint: str,
    start_batch: int | None,
    extrapolate_to_gb: float,
    crash_after_batch: int | None,
    log: Logger,
) -> None:
    catalog = build_catalog(
        catalog_uri=catalog_uri, warehouse=progress.warehouse_name, s3_endpoint=s3_endpoint
    )
    identifier = f"{progress.iceberg_namespace}.{progress.table_name}"

    done_indices = {b.index for b in progress.batches}
    resume_from = max(done_indices) + 1 if done_indices else 0
    effective_start = start_batch if start_batch is not None else resume_from
    if start_batch is not None and done_indices and start_batch <= max(done_indices):
        log.line(
            f"CẢNH BÁO: --start-batch {start_batch} <= lô đã ghi nhận cao nhất "
            f"{max(done_indices)} — lô sẽ ghi LẠI, Iceberg chỉ APPEND nên dữ liệu "
            "sẽ bị TRÙNG, không bị đè."
        )

    total = progress.total_batches_planned
    if effective_start >= total:
        log.line(f"Đã đủ {total} lô theo tiến trình đã lưu — không còn gì để ghi.")
    else:
        log.line(f"Bắt đầu từ lô {effective_start} (đã xong {len(done_indices)}/{total} lô).")

    target_bytes = progress.target_raw_gb * 1_000_000_000
    cumulative_raw_bytes = sum(b.raw_bytes for b in progress.batches if b.index < effective_start)
    # `elapsed_before`: thời gian đã cộng dồn từ CÁC LẦN GỌI TRƯỚC (nối lại sau
    # khi đứt) — không tính lại từ 0 mỗi lần, xem docstring của `Progress.elapsed_s`.
    elapsed_before = progress.elapsed_s
    run_start = time.perf_counter()

    for batch_index in range(effective_start, total):
        offset = batch_index * progress.rows_per_batch
        seed = progress.seed + batch_index + 1  # +1: seed 0 hiệu chỉnh dùng progress.seed thẳng

        t0 = time.perf_counter()
        data = make_batch(offset=offset, rows=progress.rows_per_batch, seed=seed)
        generate_s = time.perf_counter() - t0

        # Giai đoạn "biến đổi" — KHÔNG có trong phép đo này, xem docstring đầu file.
        transform_s = 0.0

        write_s, commit_s = append_batch_timed(catalog, identifier, data)

        raw_bytes = int(data.nbytes)
        record = BatchRecord(
            index=batch_index,
            rows=data.num_rows,
            raw_bytes=raw_bytes,
            generate_s=generate_s,
            transform_s=transform_s,
            iceberg_write_s=write_s,
            catalog_commit_s=commit_s,
            completed_at=datetime.now(UTC).isoformat(),
        )
        progress.batches.append(record)
        progress.elapsed_s = elapsed_before + (time.perf_counter() - run_start)
        save_progress(progress_path, progress)

        cumulative_raw_bytes += raw_bytes
        elapsed = progress.elapsed_s
        batch_wall_s = generate_s + transform_s + write_s + commit_s
        rate_mb_s = (raw_bytes / 1_000_000) / batch_wall_s if batch_wall_s > 0 else 0.0
        avg_rate_mb_s = (cumulative_raw_bytes / 1_000_000) / elapsed if elapsed > 0 else 0.0
        remaining_bytes = max(0.0, target_bytes - cumulative_raw_bytes)
        eta_s = remaining_bytes / (avg_rate_mb_s * 1_000_000) if avg_rate_mb_s > 0 else math.inf

        pct = 100 * cumulative_raw_bytes / target_bytes if target_bytes > 0 else 0.0
        log.line(
            f"[lô {batch_index + 1:>4}/{total}] dòng={data.num_rows:>9,} "
            f"thô={raw_bytes / 1e6:7.1f}MB cum={cumulative_raw_bytes / 1e9:6.3f}GB ({pct:5.1f}%) "
            f"tức_thời={rate_mb_s:7.1f}MB/s tb={avg_rate_mb_s:7.1f}MB/s eta={_format_eta(eta_s)} "
            f"| sinh={generate_s:5.2f}s biến_đổi={transform_s:5.2f}s "
            f"ghi={write_s:5.2f}s commit={commit_s:5.2f}s"
        )

        if crash_after_batch is not None and batch_index == crash_after_batch:
            log.line(
                f"--crash-after-batch={batch_index}: thoát có chủ đích (mã 99) để kiểm nối lại."
            )
            raise SystemExit(99)

    _print_summary(
        progress,
        catalog,
        identifier,
        cumulative_raw_bytes=cumulative_raw_bytes,
        elapsed=progress.elapsed_s,
        extrapolate_to_gb=extrapolate_to_gb,
        log=log,
    )


def _print_summary(
    progress: Progress,
    catalog: RestCatalog,
    identifier: str,
    *,
    cumulative_raw_bytes: int,
    elapsed: float,
    extrapolate_to_gb: float,
    log: Logger,
) -> None:
    batches = progress.batches
    generate_s = sum(b.generate_s for b in batches)
    transform_s = sum(b.transform_s for b in batches)
    write_s = sum(b.iceberg_write_s for b in batches)
    commit_s = sum(b.catalog_commit_s for b in batches)
    stage_sum = generate_s + transform_s + write_s + commit_s
    rows = sum(b.rows for b in batches)

    house = Lakehouse(catalog)
    compressed_bytes = house.scan_size_bytes(identifier)

    setup = progress.setup
    setup_sum = (
        setup.ensure_bootstrapped_s
        + setup.create_warehouse_s
        + setup.create_namespace_s
        + setup.create_table_s
        + setup.calibration_generate_s
    )

    log.line("")
    log.line("=== TỔNG KẾT ===")
    log.line(f"Lô: {len(batches)}   Dòng: {rows:,}")
    log.line(f"Thô sinh ra (byte Arrow, RAM):  {cumulative_raw_bytes / 1e9:.3f} GB")
    if compressed_bytes > 0:
        ratio = cumulative_raw_bytes / compressed_bytes
        log.line(
            f"Parquet nén trên S3 (thật):     {compressed_bytes / 1e9:.3f} GB  (nén {ratio:.2f}x)"
        )
    else:
        log.line("Parquet nén trên S3 (thật):     0 — scan_size_bytes không đọc được gì")

    def pct(x: float) -> float:
        return 100 * x / stage_sum if stage_sum > 0 else 0.0

    log.line(f"1. Sinh/đọc nguồn:  {generate_s:9.1f}s  ({pct(generate_s):5.1f}%)")
    log.line(
        f"2. Biến đổi:        {transform_s:9.1f}s  ({pct(transform_s):5.1f}%)  "
        "— KHÔNG có bước biến đổi trong phép đo này"
    )
    log.line(f"3. Ghi Iceberg:     {write_s:9.1f}s  ({pct(write_s):5.1f}%)")
    log.line(f"4. Commit catalog:  {commit_s:9.1f}s  ({pct(commit_s):5.1f}%)")
    log.line(
        f"Thiết lập một lần (bootstrap+warehouse+namespace+create_table+hiệu chỉnh): "
        f"{setup_sum:.2f}s — KHÔNG cộng vào bốn số trên"
    )
    log.line(
        f"Tổng thời gian chạy (đo thật, đồng hồ tường): {elapsed:.1f}s = {_format_eta(elapsed)}"
    )

    avg_rate_mb_s = (cumulative_raw_bytes / 1_000_000) / elapsed if elapsed > 0 else 0.0
    if avg_rate_mb_s > 0:
        extrap_s = (extrapolate_to_gb * 1_000) / avg_rate_mb_s
        log.line(
            f"Ngoại suy tuyến tính lên {extrapolate_to_gb:.0f} GB thô: ~{_format_eta(extrap_s)} "
            f"(dựa trên tốc độ TB {avg_rate_mb_s:.1f} MB/s đo được trong lần chạy NÀY)"
        )


# ─────────────────────────── dọn dẹp ────────────────────────────


def do_cleanup(
    progress_path: Path,
    *,
    management_url: str,
    catalog_uri: str,
    s3_endpoint: str,
    access_key: str,
    secret_key: str,
    log: Logger,
) -> int:
    if not progress_path.exists():
        log.line(f"Không có tiến trình nào ở '{progress_path}' — không có gì để dọn.")
        return 0

    progress = load_progress(progress_path)
    catalog = build_catalog(
        catalog_uri=catalog_uri, warehouse=progress.warehouse_name, s3_endpoint=s3_endpoint
    )
    house = Lakehouse(catalog)
    identifier = f"{progress.iceberg_namespace}.{progress.table_name}"

    try:
        house.drop_table(identifier)
        log.line(f"đã bỏ đăng ký bảng {identifier}")
    except NoSuchTableError:
        log.line(f"bảng {identifier} không còn tồn tại trong catalog (bỏ qua)")

    try:
        house.drop_namespace(progress.iceberg_namespace)
        log.line(f"đã xoá namespace {progress.iceberg_namespace}")
    except NoSuchNamespaceError:
        log.line(f"namespace {progress.iceberg_namespace} không còn tồn tại (bỏ qua)")

    with httpx.Client(timeout=30.0) as client:
        resp = client.delete(f"{management_url}/management/v1/warehouse/{progress.warehouse_id}")
    if resp.status_code not in (204, 404):
        resp.raise_for_status()
    log.line(f"đã xoá warehouse {progress.warehouse_name} ({progress.warehouse_id})")

    # Bước THẬT SỰ giải phóng đĩa — xem "CẠM BẪY DỌN DẸP" ở docstring đầu file.
    deleted = purge_s3_prefix(
        s3_endpoint=s3_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=progress.bucket,
        prefix=progress.key_prefix,
    )
    log.line(f"đã xoá {deleted} object S3 dưới prefix {progress.key_prefix}")

    progress_path.unlink()
    log.line(f"đã xoá file tiến trình {progress_path}")
    return 0


# ─────────────────────────── CLI ────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--target-raw-gb", type=float, default=50.0, help="Mục tiêu byte Arrow thô, GB thập phân"
    )
    parser.add_argument(
        "--batch-raw-mb", type=float, default=250.0, help="Kích thước thô mỗi lô, MB thập phân"
    )
    parser.add_argument("--min-free-gb", type=float, default=100.0, help="Sàn đĩa trống bắt buộc")
    parser.add_argument(
        "--check-path", type=Path, default=Path("/"), help="Filesystem kiểm đĩa trống (HOST)"
    )
    parser.add_argument("--k8s-namespace", default="loom")
    parser.add_argument(
        "--cluster-name", default="loom", help="Tên cụm k3d - mạng docker là k3d-<tên>"
    )
    parser.add_argument("--bucket", default="loom-local")
    parser.add_argument("--iceberg-namespace", default="bench_write")
    parser.add_argument("--table", default="write_path")
    parser.add_argument("--lakekeeper-port", type=int, default=8181)
    parser.add_argument("--minio-port", type=int, default=9000)
    parser.add_argument("--local-lakekeeper-port", type=int, default=28181)
    parser.add_argument("--local-minio-port", type=int, default=29000)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=REPO_ROOT / ".bench-state" / "measure-write",
        help="Nơi ghi progress.json + run.log (tail -f được)",
    )
    parser.add_argument(
        "--start-batch",
        type=int,
        default=None,
        help="Ép bỏ qua tới lô N — xem CẢNH BÁO trùng dữ liệu trong docstring đầu file",
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="Dọn bảng/namespace/warehouse/S3 của lần chạy đã lưu"
    )
    parser.add_argument("--calibration-rows", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--extrapolate-to-gb", type=float, default=50.0)
    parser.add_argument(
        "--crash-after-batch",
        type=int,
        default=None,
        help="Chỉ để KIỂM nối lại: thoát cưỡng bức (mã 99) ngay sau lô này",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir: Path = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    progress_path = state_dir / "progress.json"
    log = Logger(state_dir / "run.log")

    try:
        # Kiểm đĩa TRƯỚC BẤT CỨ THỨ GÌ khác — kể cả trước khi hỏi cụm còn sống
        # hay không. Đây là hàng rào rẻ nhất và quan trọng nhất (xem docstring
        # đầu file): một máy đầy đĩa phải bị từ chối NGAY, không phụ thuộc
        # kubectl/docker có trả lời được hay không.
        if not args.cleanup:
            required_gb = max(args.min_free_gb, args.target_raw_gb * 1.2)
            check_disk_space(args.check_path, required_gb, log)

        require_context(args.cluster_name, log)

        network = f"k3d-{args.cluster_name}"
        gateway = bridge_gateway_ip(network)
        access_key = kubectl_secret(args.k8s_namespace, "minio-root", "root-user")
        secret_key = kubectl_secret(args.k8s_namespace, "minio-root", "root-password")

        with (
            port_forward(
                k8s_namespace=args.k8s_namespace,
                service="loom-lakekeeper",
                local_port=args.local_lakekeeper_port,
                remote_port=args.lakekeeper_port,
                address="127.0.0.1",
            ),
            port_forward(
                k8s_namespace=args.k8s_namespace,
                service="minio",
                local_port=args.local_minio_port,
                remote_port=args.minio_port,
                address=gateway,
            ),
        ):
            management_url = f"http://127.0.0.1:{args.local_lakekeeper_port}"
            catalog_uri = f"{management_url}/catalog"
            s3_endpoint = f"http://{gateway}:{args.local_minio_port}"

            if args.cleanup:
                return do_cleanup(
                    progress_path,
                    management_url=management_url,
                    catalog_uri=catalog_uri,
                    s3_endpoint=s3_endpoint,
                    access_key=access_key,
                    secret_key=secret_key,
                    log=log,
                )

            if progress_path.exists():
                progress = load_progress(progress_path)
                log.line(f"Tiếp tục từ tiến trình đã lưu: {progress_path}")
            else:
                progress = fresh_setup(
                    management_url=management_url,
                    catalog_uri=catalog_uri,
                    s3_endpoint=s3_endpoint,
                    bucket=args.bucket,
                    access_key=access_key,
                    secret_key=secret_key,
                    iceberg_namespace=args.iceberg_namespace,
                    table_name=args.table,
                    target_raw_gb=args.target_raw_gb,
                    batch_raw_mb=args.batch_raw_mb,
                    calibration_rows=args.calibration_rows,
                    seed=args.seed,
                    log=log,
                )
                save_progress(progress_path, progress)

            run_batches(
                progress,
                progress_path,
                catalog_uri=catalog_uri,
                s3_endpoint=s3_endpoint,
                start_batch=args.start_batch,
                extrapolate_to_gb=args.extrapolate_to_gb,
                crash_after_batch=args.crash_after_batch,
                log=log,
            )
        return 0
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
