"""Một container MinIO cho cả session.

Container THẬT, không mock: điều đang được khẳng định là "MinIO thi hành policy
này đúng như ta nghĩ". Một mock chỉ khẳng định "ta nghĩ đúng như ta nghĩ", và
đó là loại phép kiểm mà dự án này đã tám lần phải gỡ bỏ.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from testcontainers.community.minio import MinioContainer

from loom_storage import MinioStsProvider

ROOT_USER = "loom-root"
ROOT_PASSWORD = "loom-root-test"  # container dùng một lần
BUCKET = "loom-test"


@pytest.fixture(scope="session")
def minio() -> Iterator[MinioContainer]:
    container = MinioContainer(access_key=ROOT_USER, secret_key=ROOT_PASSWORD)
    with container as running:
        yield running


@pytest.fixture(scope="session")
def endpoint(minio: MinioContainer) -> str:
    return f"http://{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"


@pytest.fixture(scope="session")
def root_s3(endpoint: str) -> Any:
    """Client toàn quyền — CHỈ dùng để dựng tiền đề, không bao giờ để khẳng định.

    Một phép kiểm ranh giới mà dùng credential gốc để đọc thì luôn xanh, và nó
    xanh vì credential sai chứ không vì ranh giới đúng.
    """
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ROOT_USER,
        aws_secret_access_key=ROOT_PASSWORD,
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture(scope="session")
def provider(endpoint: str) -> MinioStsProvider:
    return MinioStsProvider(
        endpoint_url=endpoint,
        bucket=BUCKET,
        root_access_key=ROOT_USER,
        root_secret_key=ROOT_PASSWORD,
    )


@pytest.fixture(scope="session")
def two_workspaces(root_s3: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """Hai workspace, mỗi cái có sẵn một object — dựng bằng credential GỐC.

    Phải có object thật ở CẢ HAI: một phép kiểm "A không đọc được prefix của B"
    mà prefix của B rỗng sẽ xanh kể cả khi policy mở toang, vì không có gì để đọc.
    Đúng lớp lỗi mà test escape LIKE ở Giai đoạn 1b đã mắc.
    """
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    for ws, body in ((ws_a, b"du lieu cua A"), (ws_b, b"du lieu cua B")):
        root_s3.put_object(Bucket=BUCKET, Key=f"workspaces/{ws}/Tables/t/data.parquet", Body=body)
    return ws_a, ws_b
