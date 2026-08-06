"""Hợp đồng cấp credential. KHÔNG I/O — bản cài đặt mới gọi mạng.

`StorageCredentials` là một Protocol có đúng một bản cài ở Giai đoạn 2
(`MinioStsProvider`). Nó tồn tại vì hai lý do có thật, không phải để phòng xa:
contract test ở `tests/integration/test_minio_sts.py` viết theo nó, và Giai đoạn 6
cần một đường thứ hai để xoay credential mà không sửa chỗ gọi.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


def prefix_for_workspace(workspace_id: uuid.UUID) -> str:
    """Prefix S3 của một workspace, LUÔN có dấu / ở cuối.

    Dấu / cuối không phải chi tiết định dạng: đây là chuỗi mà policy STS giới hạn
    vào, và `workspaces/<a>` không có / cũng khớp `workspaces/<a>-khac/...`.

    Dựng từ uuid chứ không từ `workspace.name`, theo quyết định của Task 21 Giai
    đoạn 1: đổi tên workspace không được làm đổi vị trí dữ liệu.
    """
    return f"workspaces/{workspace_id}/"


def prefix_for_lakehouse(workspace_id: uuid.UUID, lakehouse_id: uuid.UUID) -> str:
    """Prefix của một lakehouse — nơi `Tables/` và `Files/` nằm dưới.

    Dựng bằng cách NỐI TIẾP `prefix_for_workspace` chứ không viết lại chuỗi: policy
    STS chỉ giới hạn tới tầng workspace, nên một prefix lakehouse trượt ra ngoài
    tầng đó sẽ bị từ chối bằng 403 — một thông báo không hề nói về bố cục, và mất
    hàng giờ để lần ra.

    Bố cục đầy đủ, theo spec v1 mục 5.1:

        workspaces/{workspace_id}/lakehouses/{lakehouse_id}/Tables/{ns}/{table}/
        workspaces/{workspace_id}/lakehouses/{lakehouse_id}/Files/
    """
    return f"{prefix_for_workspace(workspace_id)}lakehouses/{lakehouse_id}/"


@dataclass(frozen=True, slots=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


class StorageCredentials(Protocol):
    """Credential đọc/ghi trong đúng prefix của một workspace."""

    def for_workspace(self, workspace_id: uuid.UUID) -> S3Credentials: ...
