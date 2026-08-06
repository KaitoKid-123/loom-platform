"""Bản cài đặt `StorageCredentials` duy nhất của Giai đoạn 2.

Chỗ DUY NHẤT trong package này gọi mạng. `credentials.py` và `policy.py` thuần
hàm, nên phần lớn hành vi test được không cần container; file này thì bắt buộc
phải có MinIO thật, và nó có — xem `tests/integration/test_minio_sts.py`.
"""

import uuid
from datetime import UTC
from typing import TYPE_CHECKING

import boto3

from loom_storage.credentials import S3Credentials, StorageCredentials
from loom_storage.policy import workspace_policy

# MinIO từ chối AssumeRole dưới 900 giây. Đặt đúng sàn: credential này sống hết
# một query rồi thôi, và mỗi giây thừa là một giây nó còn dùng lại được nếu rò.
_DURATION_SECONDS = 900

# MinIO BỎ QUA RoleArn khi dùng identity nội bộ, nhưng boto3 thì BẮT BUỘC phải
# có tham số này — thiếu nó là ParamValidationError ở phía client, chưa kịp ra
# khỏi máy. Giá trị chỉ cần đúng hình dạng một ARN.
_UNUSED_ROLE_ARN = "arn:aws:iam::000000000000:role/loom"


class MinioStsProvider:
    """Cấp credential ngắn hạn, hẹp theo prefix, qua STS AssumeRole của MinIO.

    Giữ credential GỐC — đó là điều không tránh được, vì phải có một danh tính
    nào đó ký request AssumeRole. Điều quan trọng là thứ nó PHÁT RA không bao giờ
    rộng hơn một workspace, nên `loom-query` (pod ấm dùng chung, Giai đoạn 2b)
    không bao giờ cầm trong tay một credential mở được dữ liệu của người khác.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        root_access_key: str,
        root_secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._sts = boto3.client(
            "sts",
            endpoint_url=endpoint_url,
            aws_access_key_id=root_access_key,
            aws_secret_access_key=root_secret_key,
            region_name=region,
        )

    def for_workspace(self, workspace_id: uuid.UUID) -> S3Credentials:
        response = self._sts.assume_role(
            RoleArn=_UNUSED_ROLE_ARN,
            RoleSessionName=f"loom-{workspace_id}",
            Policy=workspace_policy(self._bucket, workspace_id),
            DurationSeconds=_DURATION_SECONDS,
        )
        raw = response["Credentials"]
        # botocore trả datetime đã có tzinfo; ép về UTC để `S3Credentials` không
        # phải đoán, và để so sánh trong `is_expired` luôn cùng một hệ quy chiếu.
        return S3Credentials(
            access_key_id=raw["AccessKeyId"],
            secret_access_key=raw["SecretAccessKey"],
            session_token=raw["SessionToken"],
            expires_at=raw["Expiration"].astimezone(UTC),
        )


if TYPE_CHECKING:
    # Phép canh Protocol lúc KIỂM KIỂU, và nó phải nằm ở ĐÂY chứ không trong test:
    # `mypy.files` chỉ gồm `src`, nên cùng dòng này đặt trong `tests/` sẽ không
    # được kiểm và sẽ xanh với mọi chữ ký sai. Đã kiểm bằng thực nghiệm.
    _protocol_check: type[StorageCredentials] = MinioStsProvider
