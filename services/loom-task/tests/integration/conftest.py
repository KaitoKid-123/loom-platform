"""BỐN container: MinIO, Postgres của Lakekeeper, Lakekeeper THẬT, Postgres NGUỒN.

Các fixture container (`minio`, `s3_endpoint`, `bucket`, `catalog_pg`,
`lakekeeper`, `pinned_image`, `_bridge_gateway_ip`, `_run_migrate`,
`_wait_for_health`) là bản chép GẦN NGUYÊN VĂN của
`packages/icebergkit/tests/integration/conftest.py` — cùng cách mà
`services/loom-query/tests/integration/conftest.py` đã chép nó ở Giai đoạn 2b,
và cùng lý do: fixture đó đã giải xong bài toán mạng khó nhất (ba container nói
chuyện được với nhau VÀ với tiến trình pytest, qua IP gateway của docker bridge
mặc định). Đọc docstring đầu file gốc để biết đầy đủ cạm bẫy — quan trọng nhất là
Lakekeeper CẤP LẠI `s3.endpoint` của nó cho PyIceberg và ĐÈ lên giá trị client tự
đặt, nên `localhost` sai theo hai hướng khác nhau tuỳ phía.

**Container thứ tư là điểm khác so với hai bản chép trước: một Postgres NGUỒN.**
`loom-task` là service duy nhất đứng giữa hai hệ thống ngoài — nó ĐỌC một
database không thuộc Loom và GHI vào một lakehouse Iceberg — nên một bài test
`full` thật cần cả hai đầu là thật. Thay Postgres nguồn bằng `FakeConnector` sẽ
giết đúng nửa giá trị của bộ test này: `PostgresConnector` là bên quyết định số
lô và schema Arrow, và cú tráo bảng phải chịu được schema THẬT (`numeric` về
string, `timestamptz`, cột nullable) chứ không chỉ hai cột int64 của fake.

`seeded_source` KHÔNG lấy lại từ `packages/connectorkit/tests/conftest.py`: pytest
nối conftest theo cây thư mục và hai package là hai cây khác nhau.

**`seeded_source` trả một TUPLE kiểu built-in, và bài test KHÔNG `from .conftest
import` gì cả.** Thư mục này cố ý không có `__init__.py` (Giai đoạn 2a:
`packages/*/tests/integration/__init__.py` trùng tên module với
`services/api/tests/integration/__init__.py` và `make test-int` chết ở bước
collect với `ImportPathMismatchError`), nên một import tương đối đòi đúng cái file
đó. `packages/connectorkit/tests/integration/conftest.py` và
`packages/storagekit/...` đã dính lỗi y hệt và chọn cùng cách giải: mọi thứ một
test cần đi qua THAM SỐ CỦA FIXTURE.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import boto3
import httpx
import psycopg
import pytest
from psycopg import sql
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from loom_iceberg import Lakehouse, build_catalog, create_warehouse, ensure_bootstrapped

ROOT_USER = "loom-root"
ROOT_PASSWORD = "loom-root-test"  # container dùng một lần
BUCKET = "loom-task-test"

# Cùng tag với ba bộ integration test kia — Postgres ở đây chỉ là backing store
# của Lakekeeper (và, cho `source_postgres`, một nguồn giả lập), không phải
# Postgres của cụm, nên không pin trong deploy/versions.env.
POSTGRES_IMAGE = "postgres:17-alpine"

_LAKEKEEPER_ENCRYPTION_KEY = "loom-test-lakekeeper-encryption-key-not-for-prod"
_LAKEKEEPER_PORT = 8181

REPO_ROOT = Path(__file__).resolve().parents[4]

# Nguồn: 500 dòng, 4 cột. 500 khớp con số mà kế hoạch Task 12 dùng để nghiệm thu
# ("full hai lần cho ra ĐÚNG 500 dòng, không gấp đôi"), và `_SOURCE_BATCH` chia
# nó thành NĂM lô — đủ để `crash_after_batch=2` đứt ở GIỮA thật, chứ không ở lô
# cuối (một crash sau lô cuối không phân biệt được với một lần chạy xong).
_SOURCE_SCHEMA = "shop"
_SOURCE_STREAM = "shop.orders"
_SOURCE_ROWS = 500
_SOURCE_BATCH = 100


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
    """Bucket dựng bằng credential GỐC, TRƯỚC khi warehouse nào được tạo.

    Lakekeeper trả 424 nếu bucket chưa tồn tại — warehouse chỉ dựng key-prefix
    bên trong một bucket có sẵn, nó không tạo bucket.
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
    """Poll `/health` tới khi 200 — KHÔNG `sleep` cố định (xem file gốc)."""
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


# ------------------------------------------------------- Nguồn bị nạp


@pytest.fixture(scope="session")
def _source_postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer(image=POSTGRES_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def seeded_source(_source_postgres: PostgresContainer) -> tuple[str, str, int, int]:
    """Postgres NGUỒN đã seed. Trả `(dsn, stream, số dòng, số dòng mỗi lô)`.

    Trả TUPLE built-in chứ không một dataclass khai ở đây — xem docstring đầu
    file: bài test là file ANH EM của conftest này và không import được từ nó.

    Scope session và seed MỘT lần: mọi bài trong bộ này chỉ ĐỌC nguồn (`full`
    không ghi gì về nguồn), nên không bài nào làm bẩn dữ liệu của bài khác, và
    một container cho cả session là vài giây tiết kiệm thật.

    Bốn cột cố ý không đồng nhất kiểu: `amount numeric` về Arrow string (xem
    `PostgresConnector`), `placed_at timestamptz`, `note` nullable. Cú tráo bảng
    phải chịu được một schema thật — với bốn cột int64 giống nhau, một lỗi chuyển
    schema ở bước `create_from` sẽ không lộ ra.
    """
    pg = _source_postgres
    dsn = (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    )
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(_SOURCE_SCHEMA))
        )
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(_SOURCE_SCHEMA)))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.orders ("
                "id integer NOT NULL, "
                "placed_at timestamptz NOT NULL, "
                "amount numeric(12,2) NOT NULL, "
                "note text)"
            ).format(sql.Identifier(_SOURCE_SCHEMA))
        )
        base = datetime(2024, 1, 1, tzinfo=UTC)
        cur.executemany(
            sql.SQL(
                "INSERT INTO {}.orders (id, placed_at, amount, note) VALUES (%s, %s, %s, %s)"
            ).format(sql.Identifier(_SOURCE_SCHEMA)),
            [
                (
                    i,
                    base + timedelta(minutes=i),
                    Decimal("19.99") + i,
                    None if i % 6 == 0 else f"n{i}",
                )
                for i in range(_SOURCE_ROWS)
            ],
        )
    return dsn, _SOURCE_STREAM, _SOURCE_ROWS, _SOURCE_BATCH


# ------------------------------------------------------- Lakehouse đích


@pytest.fixture
def lakehouse_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def warehouse_name(lakekeeper: str, s3_endpoint: str, bucket: str, lakehouse_id: uuid.UUID) -> str:
    """Một warehouse RIÊNG cho mỗi test, đặt tên theo `str(lakehouse_id)`.

    Tên theo id là quy ước mà `main._build_sink` giả định (cùng quy ước
    `loom_query.runner` dùng để đọc). Một warehouse riêng cho mỗi test là điều
    làm các bài `full` độc lập: bài này để lại bảng `..._old_<hex>` hay
    `..._staging_<hex>` cũng không lọt vào `list_tables` của bài kia.
    """
    name = str(lakehouse_id)
    create_warehouse(
        lakekeeper,
        name=name,
        bucket=bucket,
        key_prefix=f"loom-task-test/{lakehouse_id}",
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
