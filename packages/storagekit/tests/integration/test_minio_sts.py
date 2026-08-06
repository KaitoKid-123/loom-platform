"""Contract test của `StorageCredentials`.

Một khẳng định, và nó là lý do Giai đoạn 2 chọn MinIO thay vì Backblaze B2:

    Credential của workspace A không đọc được prefix của workspace B.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from loom_storage import MinioStsProvider, S3Credentials

from .conftest import BUCKET, pinned_image

pytestmark = pytest.mark.integration


def test_container_runs_the_same_minio_as_the_cluster(minio: object) -> None:
    """Contract test bên dưới chỉ có giá trị nếu nó chạy đúng MinIO mà cụm chạy.

    Bản đầu của conftest để `MinioContainer` dùng image mặc định của nó —
    `RELEASE.2022-12-02` — trong khi `deploy/versions.env` pin `RELEASE.2025-04-22`
    cho cụm. Sáu phép kiểm bên dưới đều XANH, và không phép nào nhìn thấy chuyện
    chúng đang kiểm một MinIO cách bản thật hai năm rưỡi.

    Phép này là thứ chặn nó trôi lại: đổi image của container mà quên `versions.env`
    (hoặc ngược lại) thì nó đỏ ngay.
    """
    running = getattr(minio, "image", None)
    assert running == pinned_image("MINIO_IMAGE"), (
        f"container chạy {running!r} nhưng cụm chạy {pinned_image('MINIO_IMAGE')!r} — "
        "contract test đang khẳng định về một MinIO khác với MinIO giữ dữ liệu"
    )


def _client(endpoint: str, creds: S3Credentials) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        aws_session_token=creds.session_token,
        region_name="us-east-1",
    )


def test_credentials_read_their_own_prefix(
    provider: MinioStsProvider, endpoint: str, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Vế KHẲNG ĐỊNH. Không có nó, một policy từ chối tất cả cũng làm mọi phép
    kiểm từ chối bên dưới xanh hết."""
    ws_a, _ = two_workspaces
    client = _client(endpoint, provider.for_workspace(ws_a))
    body = client.get_object(Bucket=BUCKET, Key=f"workspaces/{ws_a}/Tables/t/data.parquet")
    assert body["Body"].read() == b"du lieu cua A"


def test_credentials_cannot_read_another_workspace(
    provider: MinioStsProvider, endpoint: str, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Vế PHỦ ĐỊNH — hợp đồng thật sự."""
    ws_a, ws_b = two_workspaces
    client = _client(endpoint, provider.for_workspace(ws_a))
    with pytest.raises(ClientError) as caught:
        client.get_object(Bucket=BUCKET, Key=f"workspaces/{ws_b}/Tables/t/data.parquet")
    assert caught.value.response["Error"]["Code"] == "AccessDenied"


def test_credentials_cannot_write_into_another_workspace(
    provider: MinioStsProvider, endpoint: str, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Ghi tách riêng khỏi đọc: policy cấp `GetObject` và `PutObject` bằng hai mục
    trong cùng một Action list, nên một bản sửa làm hỏng đúng một trong hai là
    chuyện xảy ra được."""
    ws_a, ws_b = two_workspaces
    client = _client(endpoint, provider.for_workspace(ws_a))
    with pytest.raises(ClientError) as caught:
        client.put_object(Bucket=BUCKET, Key=f"workspaces/{ws_b}/xam-pham", Body=b"x")
    assert caught.value.response["Error"]["Code"] == "AccessDenied"


def test_listing_shows_only_the_own_prefix(
    provider: MinioStsProvider, endpoint: str, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Cái bẫy `ListBucket` từ Task 4, lần này kiểm trên MinIO thật.

    Liệt kê KHÔNG prefix: đây đúng là thứ một kẻ tò mò sẽ gõ. Nếu Condition trên
    `s3:prefix` thiếu, lệnh này trả về tên bảng của cả hai workspace và phép kiểm
    đỏ — dù không object nào của B đọc được.
    """
    ws_a, ws_b = two_workspaces
    client = _client(endpoint, provider.for_workspace(ws_a))
    with pytest.raises(ClientError) as caught:
        client.list_objects_v2(Bucket=BUCKET)
    assert caught.value.response["Error"]["Code"] == "AccessDenied"

    scoped = client.list_objects_v2(Bucket=BUCKET, Prefix=f"workspaces/{ws_a}/")
    keys = [o["Key"] for o in scoped.get("Contents", [])]
    assert keys == [f"workspaces/{ws_a}/Tables/t/data.parquet"]
    assert all(str(ws_b) not in k for k in keys)


def test_credentials_are_short_lived(
    provider: MinioStsProvider, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """ "Ngắn hạn" phải là một sự thật đo được, không phải một tính từ trong tài
    liệu. Không có phép kiểm này, đặt DurationSeconds thành 100 năm vẫn xanh hết."""
    ws_a, _ = two_workspaces
    creds = provider.for_workspace(ws_a)
    remaining = (creds.expires_at - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= 3600, f"credential song {remaining}s — qua dai cho mot query"
    assert not creds.is_expired()


def test_expired_credentials_are_rejected_by_the_server(
    endpoint: str, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Không đợi 15 phút — hỏi thẳng MinIO bằng một session token BỊA.

    Phép kiểm này không đo thời gian; nó khẳng định MinIO thật sự XÁC THỰC session
    token thay vì chỉ đọc access key. Một máy chủ bỏ qua token sẽ cho qua, và lúc
    đó "credential ngắn hạn" là vô nghĩa vì phần mang hạn nằm trong token.
    """
    # GIỚI HẠN, ghi ra thay vì để người đọc sau tưởng đã phủ: phép này KHÔNG
    # chứng minh một credential THẬT hết hạn sau đúng 900 giây. Nó chứng minh
    # session token được xác thực — điều kiện cần của việc hết hạn có tác dụng.
    # Kiểm cái còn lại cần chờ 15 phút thật, nên nó là một mục trong phép đo 1
    # (Task 14) chứ không nằm trong bộ test chạy mỗi lần push.
    ws_a, _ = two_workspaces
    forged = S3Credentials(
        access_key_id="AKIAFAKEFAKEFAKEFAKE",
        secret_access_key="bimatgia",  # bia, khong phai bi mat thuc
        session_token="token-bia-hoan-toan",
        expires_at=datetime.now(UTC).replace(year=2030),
    )
    client = _client(endpoint, forged)
    with pytest.raises(ClientError) as caught:
        client.get_object(Bucket=BUCKET, Key=f"workspaces/{ws_a}/Tables/t/data.parquet")
    assert caught.value.response["Error"]["Code"] in {
        "InvalidAccessKeyId",
        "AccessDenied",
        "SignatureDoesNotMatch",
    }
