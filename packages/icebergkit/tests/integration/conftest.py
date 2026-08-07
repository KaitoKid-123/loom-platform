"""Ba container dùng chung cho cả session: MinIO, Postgres, Lakekeeper THẬT.

Lakekeeper THẬT, không phải `SqlCatalog` của PyIceberg. Chúng khác nhau ở đúng
chỗ quan trọng nhất — tính nguyên tử của commit — và
`test_concurrent_commit.py` là cửa chặn của giai đoạn: nó phải khẳng định về
khoá PHÍA LAKEKEEPER, không phải khoá trong tiến trình Python mà một
`SqlCatalog` giữ hộ.

## Cạm bẫy mạng

Lakekeeper cấp cho client cái `s3.endpoint` LẤY TỪ storage-profile của
warehouse và ĐÈ LÊN bất cứ giá trị nào client tự đặt (đã xác minh bằng thực
nghiệm trên Lakekeeper v0.9.2). Endpoint đó phải là một địa chỉ mà CẢ HAI phía
với tới được:

  - tiến trình pytest, chạy trên HOST
  - container Lakekeeper, chạy trong DOCKER

`localhost` không dùng được: trong container Lakekeeper, `localhost` là chính
nó, không phải MinIO.

Cách chạy được, đã kiểm bằng `curl` từ host và từ một container khác TRƯỚC khi
viết bộ test này (`docker network inspect bridge`, rồi `curl` tới
`http://<gateway>:<port>/minio/health/live` từ cả hai phía — cả hai trả 200):
địa chỉ GATEWAY của docker bridge MẶC ĐỊNH (`172.17.0.1` trên máy Linux
thường gặp), cộng cổng MinIO đã map ra host. Host với tới được vì đó là
interface `docker0` của chính nó; container với tới được vì đó là gateway của
NÓ — với điều kiện container đó cũng đứng trên bridge mặc định, không phải một
docker network riêng (một network riêng có gateway KHÁC, không trỏ vào MinIO).
Đây là lý do Postgres và Lakekeeper dưới đây cũng cố ý KHÔNG dùng network
riêng: cả ba container — MinIO, Postgres, Lakekeeper — đứng chung trên bridge
mặc định, và container nói với nhau bằng địa chỉ IP nội bộ (bridge mặc định
không gán tên DNS cho container).
"""

import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import boto3
import httpx
import pytest
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped
from loom_storage import prefix_for_lakehouse

ROOT_USER = "loom-root"
ROOT_PASSWORD = "loom-root-test"  # container dùng một lần
BUCKET = "loom-test"

# Cùng tag với services/api/tests/integration/pg_support.py — không pin trong
# deploy/versions.env vì Postgres ở đây chỉ là backing store của Lakekeeper
# trong test, không phải Postgres của cụm.
POSTGRES_IMAGE = "postgres:17-alpine"

# Hằng test, KHÔNG phải secret thực — Lakekeeper dùng nó để mã hoá credential
# lưu trong Postgres, và container này bị xoá cuối session.
_LAKEKEEPER_ENCRYPTION_KEY = "loom-test-lakekeeper-encryption-key-not-for-prod"
_LAKEKEEPER_PORT = 8181

REPO_ROOT = Path(__file__).resolve().parents[4]


def pinned_image(key: str) -> str:
    """Đọc tag image từ `deploy/versions.env` — cùng file mà Makefile `include`.

    Bản sao GIỮ NGUYÊN hành vi của `packages/storagekit/tests/integration/
    conftest.py`: cả hai đọc thẳng cùng một file nguồn bằng cùng cách, nên
    không thể lệch hành vi dù mã nguồn trùng nhau. Tách ra một module dùng
    chung là việc làm được, nhưng nằm ngoài phạm vi của package này.
    """
    for line in (REPO_ROOT / "deploy" / "versions.env").read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip()
    raise RuntimeError(f"{key} không có trong deploy/versions.env")


def _bridge_gateway_ip() -> str:
    """IP gateway của docker bridge mặc định — xem docstring đầu file."""
    # "docker" chạy qua PATH có chủ đích, không nhận input từ ngoài.
    result = subprocess.run(
        ["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="session")
def minio() -> Iterator[MinioContainer]:
    container = MinioContainer(
        image=pinned_image("MINIO_IMAGE"),
        access_key=ROOT_USER,
        secret_key=ROOT_PASSWORD,
    )
    with container as running:
        yield running


@pytest.fixture(scope="session")
def s3_endpoint(minio: MinioContainer) -> str:
    """Endpoint MinIO qua gateway bridge — dùng được cả từ host và từ container.

    KHÔNG dùng `minio.get_container_host_ip()` (trả `localhost`): giá trị đó
    đúng cho pytest nhưng vô nghĩa cho Lakekeeper, vì Lakekeeper CẤP LẠI đúng
    chuỗi endpoint này cho PyIceberg (xem docstring đầu file) — một `localhost`
    cấp cho PyIceberg sẽ trỏ vào chính tiến trình pytest, không phải MinIO.
    """
    gateway = _bridge_gateway_ip()
    port = minio.get_exposed_port(minio.port)
    return f"http://{gateway}:{port}"


@pytest.fixture(scope="session")
def bucket(s3_endpoint: str) -> str:
    """Bucket dựng bằng credential GỐC, TRƯỚC khi warehouse nào được tạo.

    Lakekeeper trả 424 `FileWriterCreation` (S3 `NoSuchBucket`) nếu bucket
    chưa tồn tại — đã kiểm bằng thực nghiệm. Warehouse chỉ dựng KEY-PREFIX bên
    trong một bucket có sẵn, không tự tạo bucket.
    """
    client = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=ROOT_USER,
        aws_secret_access_key=ROOT_PASSWORD,
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=BUCKET)
    return BUCKET


@pytest.fixture(scope="session")
def catalog_pg() -> Iterator[PostgresContainer]:
    """Postgres của chính Lakekeeper — schema do `lakekeeper migrate` dựng,
    không phải Postgres của services/api."""
    with PostgresContainer(
        POSTGRES_IMAGE, username="lakekeeper", password="lakekeeper", dbname="lakekeeper"
    ) as pg:
        yield pg


def _lakekeeper_env(pg: PostgresContainer) -> dict[str, str]:
    """Trỏ Lakekeeper vào Postgres bằng IP BRIDGE của chính container Postgres.

    Không dùng cổng đã map ra host: đó là địa chỉ cho pytest, còn Lakekeeper
    nói chuyện với Postgres HOÀN TOÀN trong docker, qua cổng nội bộ 5432. Cả
    hai container đứng trên bridge mặc định (không network riêng — xem
    docstring đầu file), nên IP nội bộ của Postgres là địa chỉ Lakekeeper
    dùng được thẳng.
    """
    pg_ip = pg.get_docker_client().bridge_ip(pg.get_container_id())
    return {
        "LAKEKEEPER__PG_HOST_R": pg_ip,
        "LAKEKEEPER__PG_HOST_W": pg_ip,
        "LAKEKEEPER__PG_PORT": "5432",
        "LAKEKEEPER__PG_USER": pg.username,
        "LAKEKEEPER__PG_PASSWORD": pg.password,
        "LAKEKEEPER__PG_DATABASE": pg.dbname,
        "LAKEKEEPER__PG_ENCRYPTION_KEY": _LAKEKEEPER_ENCRYPTION_KEY,
    }


def _run_migrate(image: str, env: dict[str, str]) -> None:
    """`lakekeeper migrate` — container dùng MỘT LẦN, chạy xong dọn ngay.

    `serve` (fixture `lakekeeper` bên dưới) đòi database đã migrate; gọi
    `serve` trước khi migrate xong chỉ nhận lỗi kết nối không nói gì về
    nguyên nhân thật.
    """
    migrator = DockerContainer(image, command="migrate", env=dict(env))
    migrator.start()
    try:
        exit_code = migrator.wait()
        stdout, stderr = migrator.get_logs()
    finally:
        migrator.stop()
    if exit_code != 0:
        raise RuntimeError(
            f"lakekeeper migrate thoat voi code {exit_code}\n"
            f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )


def _wait_for_health(base_url: str, timeout_seconds: float = 60.0) -> None:
    """Poll `/health` tới khi 200 — KHÔNG `sleep` cố định.

    Một `sleep(N)` cố định luôn sai theo một hướng: thừa trên máy nhanh (chờ
    dư mỗi lần chạy), hoặc thiếu trên máy chậm/CI tải cao (đỏ ngẫu nhiên do
    thời điểm, không do lỗi thật). Poll tới đúng điều kiện là cách duy nhất
    không đoán khoảng thời gian.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error = "chua thu lan nao"
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/health")
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}: {response.text}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(0.5)
    raise TimeoutError(f"Lakekeeper khong san sang sau {timeout_seconds}s: {last_error}")


@pytest.fixture(scope="session")
def lakekeeper(catalog_pg: PostgresContainer) -> Iterator[str]:
    """URL gốc của một Lakekeeper THẬT: đã `migrate`, đang `serve`, đã bootstrap.

    Một container `serve` DUY NHẤT sống hết session — `create_warehouse` (qua
    fixture `warehouse_name`) chạy riêng cho từng test, không cần Lakekeeper
    mới mỗi lần.
    """
    image = pinned_image("LAKEKEEPER_IMAGE")
    env = _lakekeeper_env(catalog_pg)
    _run_migrate(image, env)

    serve = DockerContainer(image, command="serve", env=dict(env), ports=[_LAKEKEEPER_PORT])
    with serve as running:
        host = running.get_container_host_ip()
        port = running.get_exposed_port(_LAKEKEEPER_PORT)
        base_url = f"http://{host}:{port}"
        _wait_for_health(base_url)
        ensure_bootstrapped(base_url)
        yield base_url


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def warehouse_name(lakekeeper: str, s3_endpoint: str, bucket: str, workspace_id: uuid.UUID) -> str:
    """Một warehouse RIÊNG cho mỗi test — tên duy nhất, key-prefix riêng.

    Test dùng chung một warehouse sẽ thấy bảng/namespace của test khác trong
    cùng listing hoặc scan — đúng lớp nhiễu mà prefix S3 dùng chung đã gây ra
    ở Giai đoạn 1b, lần này ở tầng warehouse thay vì tầng object.
    """
    lakehouse_id = uuid.uuid4()
    # `.rstrip("/")`: body tạo warehouse đã xác minh chạy dùng key-prefix
    # KHÔNG có dấu / cuối, khác với `prefix_for_workspace`/`prefix_for_lakehouse`
    # của storagekit (luôn có / cuối, vì đó là chuỗi cho policy STS — một mục
    # đích khác).
    key_prefix = prefix_for_lakehouse(workspace_id, lakehouse_id).rstrip("/")
    name = f"loom-test-{uuid.uuid4().hex}"
    create_warehouse(
        lakekeeper,
        name=name,
        bucket=bucket,
        key_prefix=key_prefix,
        s3_endpoint=s3_endpoint,
        access_key=ROOT_USER,
        secret_key=ROOT_PASSWORD,
    )
    return name


@pytest.fixture
def lakehouse(lakekeeper: str, s3_endpoint: str, warehouse_name: str) -> Lakehouse:
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    return Lakehouse(catalog)
