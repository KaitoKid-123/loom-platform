"""Task 13 (Giai đoạn 2b) — đọc file thô trong `Files/`, kiểm với MinIO THẬT.

**Không cần Lakekeeper/Postgres.** Một query CHỈ đọc `Files/` không bao giờ mở
catalog Iceberg nào — `resolved_tables=()` khiến vòng lặp đăng ký bảng và
`check_scan_bytes` trong `runner._run_sync` đều rỗng vô hại (xem docstring ở
đó). File này vì vậy chỉ dùng `minio`/`s3_endpoint`/`bucket` (session-scoped,
đã chạy cho các file khác trong `tests/integration/`) — KHÔNG kéo theo
`lakekeeper`/`catalog_pg`, giữ đúng ngân sách RAM/thời gian của bộ test tích
hợp (spec Giai đoạn 2b).

Hai bài trong file này là chứng minh đỏ 3 và vế KHẲNG ĐỊNH (chứng minh 4) của
Task 13 — xem báo cáo hoàn tất Task 13 cho toàn bộ năm chứng minh đỏ (hai bài
còn lại, thoát prefix và bỏ kiểm quyền, là test THUẦN không cần Docker, xem
`tests/test_files.py` và `tests/test_query_authz_gate.py`).
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Any

import boto3
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from loom_core.schemas import Principal
from loom_query import runner
from loom_query.config import Settings
from loom_query.main import create_app
from loom_storage import MinioStsProvider, prefix_for_lakehouse

from ..conftest import FakeAuthz, a_principal, http_client
from .conftest import ROOT_PASSWORD, ROOT_USER

pytestmark = pytest.mark.integration

_SAMPLE_ROWS = {"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}
_UNREACHABLE = (
    "http://127.0.0.1:1"  # cổng 1 — không service nào lắng nghe, xem test_query_routes.py
)


def _body(
    lakehouse_id: uuid.UUID, workspace_id: uuid.UUID, sql: str, principal: Principal
) -> dict[str, Any]:
    return {
        "lakehouse_id": str(lakehouse_id),
        "workspace_id": str(workspace_id),
        "sql": sql,
        "principal": {
            "user_id": str(principal.user_id),
            "subject": principal.subject,
            "email": principal.email,
            "display_name": principal.display_name,
            "groups": list(principal.groups),
        },
    }


@pytest.fixture
def files_settings(s3_endpoint: str, bucket: str) -> Settings:
    """`catalog_uri` trỏ vào một cổng KHÔNG lắng nghe — một query chỉ đọc
    `Files/` không bao giờ mở nó, và nếu một lỗi tương lai LỠ mở nó, phép kiểm
    sẽ đỏ ầm ĩ (lỗi kết nối) thay vì lặng lẽ dùng nhầm Lakekeeper thật."""
    return Settings(
        catalog_uri=f"{_UNREACHABLE}/catalog",
        s3_endpoint=s3_endpoint,
        storage_bucket=bucket,
        storage_root_access_key=ROOT_USER,
        storage_root_secret_key=ROOT_PASSWORD,
    )


def _upload_sample_parquet(
    s3_endpoint: str,
    bucket: str,
    workspace_id: uuid.UUID,
    lakehouse_id: uuid.UUID,
    relative_path: str,
) -> None:
    """Đẩy một file Parquet mẫu lên ĐÚNG vị trí `Files/` của `lakehouse_id`,
    bằng credential GỐC (test setup — không phải đường mà `loom-query` dùng)."""
    table = pa.table(
        {
            "id": pa.array(_SAMPLE_ROWS["id"], type=pa.int64()),
            "amount": pa.array(_SAMPLE_ROWS["amount"], type=pa.float64()),
        }
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    client = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=ROOT_USER,
        aws_secret_access_key=ROOT_PASSWORD,
        region_name="us-east-1",
    )
    key = f"{prefix_for_lakehouse(workspace_id, lakehouse_id)}{relative_path}"
    client.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())


async def _run_and_wait(
    app: Any, lakehouse_id: uuid.UUID, workspace_id: uuid.UUID, sql: str, principal: Principal
) -> dict[str, Any]:
    async with http_client(app) as client:
        create_response = await client.post(
            "/api/v1/query", json=_body(lakehouse_id, workspace_id, sql, principal)
        )
        assert create_response.status_code == 202, create_response.text
        query_id = create_response.json()["query_id"]

        body: dict[str, Any] = {}
        for _ in range(200):
            status_response = await client.get(f"/api/v1/query/{query_id}")
            body = status_response.json()
            if body["status"] != "running":
                break
            await asyncio.sleep(0.05)
    return body


# --------------------------------------------------------------- Chứng minh 4


async def test_read_parquet_under_files_runs_and_returns_real_rows(
    files_settings: Settings, s3_endpoint: str, bucket: str, fake_authz: FakeAuthz
) -> None:
    """Vế KHẲNG ĐỊNH bắt buộc của Task 13: một `read_parquet('Files/…')` hợp
    lệ, với đủ quyền, phải CHẠY và trả DÒNG THẬT — qua đúng đường sản xuất
    (`POST /api/v1/query` -> `run_gate` -> task nền -> `GET`), không phải một
    lời gọi trực tiếp vào `runner._run_sync` bỏ qua HTTP.

    Không có bài này, ba chứng minh đỏ khác (thoát prefix, bỏ kiểm quyền,
    credential quá rộng) cũng XANH với một bản cài từ chối MỌI query — và lúc
    đó tính năng đọc `Files/` không hề tồn tại (xem cảnh báo đầu báo cáo hoàn
    tất Task 13)."""
    principal = a_principal()
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    _upload_sample_parquet(
        s3_endpoint, bucket, workspace_id, lakehouse_id, "Files/thang-01/a.parquet"
    )
    fake_authz.grant(lakehouse_id, "viewer")

    app = create_app(settings=files_settings, authz=fake_authz)
    sql = "SELECT * FROM read_parquet('Files/thang-01/a.parquet') ORDER BY id"
    body = await _run_and_wait(app, lakehouse_id, workspace_id, sql, principal)

    assert body["status"] == "succeeded", body
    assert body["rows"] == [[1, 10.0], [2, 20.0], [3, 30.0]]


async def test_read_parquet_with_a_glob_across_multiple_files(
    files_settings: Settings, s3_endpoint: str, bucket: str, fake_authz: FakeAuthz
) -> None:
    """Đúng ví dụ trong bảng nghiệm thu của spec Task 13:
    `read_parquet('Files/thang-01/*.parquet')` — nhiều file, một glob."""
    principal = a_principal()
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    _upload_sample_parquet(
        s3_endpoint, bucket, workspace_id, lakehouse_id, "Files/thang-01/a.parquet"
    )
    fake_authz.grant(lakehouse_id, "viewer")

    app = create_app(settings=files_settings, authz=fake_authz)
    sql = "SELECT count(*) AS n FROM read_parquet('Files/thang-01/*.parquet')"
    body = await _run_and_wait(app, lakehouse_id, workspace_id, sql, principal)

    assert body["status"] == "succeeded", body
    assert body["rows"] == [[3]]


# --------------------------------------------------------------- Chứng minh đỏ 3


async def test_a_files_secret_scoped_to_the_wrong_workspace_is_denied_by_minio(
    files_settings: Settings, s3_endpoint: str, bucket: str
) -> None:
    """Chứng minh đỏ 3 của Task 13: cấp credential của một WORKSPACE KHÁC,
    mô phỏng đúng lỗi "cấp nhầm workspace" mà `runner._install_files_secret`
    có thể mắc phải nếu `workspace_id` bị lẫn lộn ở đâu đó.

    Đường dẫn S3 THẬT SỰ đọc là CỐ ĐỊNH, ĐÚNG (`correct_uri`, dựng từ
    `workspace_id` thật) trong CẢ HAI lần gọi — biến DUY NHẤT là workspace mà
    credential được cấp CHO. Điều đó tách bạch: nếu bài này đỏ vì lý do khác
    (path sai, bucket sai...), đó không chứng minh ranh giới CREDENTIAL — nó
    chỉ chứng minh đường dẫn tình cờ đúng. Ở đây path luôn đúng, nên một 403
    chỉ có thể tới từ MinIO từ chối chính CREDENTIAL.

    Gọi thẳng `runner._install_files_secret` — ĐÚNG hàm sản xuất, không phải
    một bản dựng lại credential/SECRET riêng cho test — nên bài này canh được
    một hồi quy thật nếu `_install_files_secret` sau này đổi cách cấp phạm vi.
    """
    workspace_id = uuid.uuid4()
    other_workspace_id = uuid.uuid4()  # workspace KHÁC, không liên quan gì
    lakehouse_id = uuid.uuid4()
    _upload_sample_parquet(s3_endpoint, bucket, workspace_id, lakehouse_id, "Files/a.parquet")

    storage = MinioStsProvider(
        endpoint_url=s3_endpoint,
        bucket=bucket,
        root_access_key=ROOT_USER,
        root_secret_key=ROOT_PASSWORD,
    )
    correct_uri = f"s3://{bucket}/{prefix_for_lakehouse(workspace_id, lakehouse_id)}Files/a.parquet"

    def read_with(scoped_workspace_id: uuid.UUID) -> list[tuple[Any, ...]]:
        connection = duckdb.connect(":memory:")
        try:
            runner._install_files_secret(
                connection,
                storage=storage,
                workspace_id=scoped_workspace_id,
                settings=files_settings,
            )
            # S608 báo động giả: `correct_uri` dựng từ giá trị test tự sinh
            # (`uuid.uuid4()`/`bucket` cố định), không phải input người dùng.
            return connection.sql(
                f"SELECT * FROM read_parquet('{correct_uri}') ORDER BY id"  # noqa: S608
            ).fetchall()
        finally:
            connection.close()

    # Đúng workspace: đọc được — chốt chống-xanh-rỗng, xem docstring module.
    assert read_with(workspace_id) == [(1, 10.0), (2, 20.0), (3, 30.0)]

    # Credential của workspace KHÁC, ĐÚNG path — phải đỏ VÌ MINIO TỪ CHỐI.
    with pytest.raises(duckdb.HTTPException) as exc_info:
        read_with(other_workspace_id)
    message = str(exc_info.value)
    assert "403" in message or "AccessDenied" in message or "Access Denied" in message, (
        f"kỳ vọng lỗi 403/AccessDenied từ MinIO, thấy: {message}"
    )
