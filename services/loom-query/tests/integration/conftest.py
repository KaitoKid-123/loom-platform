"""Container thật cho test CẦN Docker: MinIO + Postgres + Lakekeeper.

Các fixture container (`minio`, `s3_endpoint`, `bucket`, `catalog_pg`,
`lakekeeper`, `pinned_image`, `_bridge_gateway_ip`, `_run_migrate`,
`_wait_for_health`) là bản chép GẦN NGUYÊN VĂN của
`packages/icebergkit/tests/integration/conftest.py`, đúng như spec Giai đoạn
2b yêu cầu ("Chép cách làm đó, đừng phát minh lại"): fixture đó đã giải xong
bài toán mạng khó nhất — ba container nói chuyện được với nhau VÀ với tiến
trình pytest, qua IP gateway của docker bridge mặc định. Đọc docstring đầu
file gốc để biết đầy đủ cạm bẫy (endpoint Lakekeeper CẤP LẠI cho PyIceberg,
`localhost` sai theo hai hướng khác nhau tuỳ phía).

Phần RIÊNG của `loom-query` ở cuối file: một warehouse đặt tên theo
`lakehouse_id` (đúng quy ước tạm mà `loom_query.config.Settings` giả định —
xem `runner.py`), một bảng Iceberg thật có dữ liệu, và `Settings` trỏ vào cụm
container này.
"""

import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import boto3
import httpx
import pyarrow as pa
import pytest
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped
from loom_query.config import Settings

ROOT_USER = "loom-root"
ROOT_PASSWORD = "loom-root-test"  # container dùng một lần
BUCKET = "loom-query-test"

# Cùng tag với packages/icebergkit/tests/integration/conftest.py — không pin
# trong deploy/versions.env vì Postgres ở đây chỉ là backing store của
# Lakekeeper trong test, không phải Postgres của cụm.
POSTGRES_IMAGE = "postgres:17-alpine"

_LAKEKEEPER_ENCRYPTION_KEY = "loom-test-lakekeeper-encryption-key-not-for-prod"
_LAKEKEEPER_PORT = 8181

REPO_ROOT = Path(__file__).resolve().parents[4]


def pinned_image(key: str) -> str:
    """Đọc tag image từ `deploy/versions.env` — cùng file mà Makefile `include`."""
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
    """Endpoint MinIO qua gateway bridge — dùng được cả từ host và từ container."""
    gateway = _bridge_gateway_ip()
    port = minio.get_exposed_port(minio.port)
    return f"http://{gateway}:{port}"


@pytest.fixture(scope="session")
def bucket(s3_endpoint: str) -> str:
    """Bucket dựng bằng credential GỐC, TRƯỚC khi warehouse nào được tạo."""
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
    """Postgres của chính Lakekeeper — schema do `lakekeeper migrate` dựng."""
    with PostgresContainer(
        POSTGRES_IMAGE, username="lakekeeper", password="lakekeeper", dbname="lakekeeper"
    ) as pg:
        yield pg


def _lakekeeper_env(pg: PostgresContainer) -> dict[str, str]:
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


# ------------------------------------------------------- Phần riêng của loom-query


@pytest.fixture
def lakehouse_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def warehouse_name(lakekeeper: str, s3_endpoint: str, bucket: str, lakehouse_id: uuid.UUID) -> str:
    """Warehouse đặt tên theo `str(lakehouse_id)` — đúng quy ước mà
    `runner._run_sync` giả định (xem docstring của nó): warehouse của
    Lakekeeper đặt tên theo `item.id`, không theo `item.name`."""
    name = str(lakehouse_id)
    create_warehouse(
        lakekeeper,
        name=name,
        bucket=bucket,
        key_prefix=f"loom-query-test/{lakehouse_id}",
        s3_endpoint=s3_endpoint,
        access_key=ROOT_USER,
        secret_key=ROOT_PASSWORD,
    )
    return name


@pytest.fixture
def seeded_table(lakekeeper: str, s3_endpoint: str, warehouse_name: str) -> str:
    """Một bảng Iceberg THẬT, `sales.orders`, ba dòng — dữ liệu mà phép kiểm
    SELECT đơn giản đọc lại qua HTTP. Trả về tên hai phần `namespace.table`."""
    catalog = build_catalog(
        catalog_uri=f"{lakekeeper}/catalog", warehouse=warehouse_name, s3_endpoint=s3_endpoint
    )
    lakehouse = Lakehouse(catalog)
    lakehouse.create_namespace("sales")
    qualified = "sales.orders"
    lakehouse.create_from(
        qualified,
        pa.table(
            {
                "id": pa.array([1, 2, 3], type=pa.int64()),
                "amount": pa.array([10.0, 20.0, 30.0], type=pa.float64()),
            }
        ),
    )
    return qualified


@pytest.fixture
def app_settings(lakekeeper: str, s3_endpoint: str) -> Settings:
    return Settings(catalog_uri=f"{lakekeeper}/catalog", s3_endpoint=s3_endpoint)
