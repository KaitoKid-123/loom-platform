import uuid
from datetime import UTC, datetime, timedelta

import pytest

from loom_storage.credentials import (
    S3Credentials,
    prefix_for_lakehouse,
    prefix_for_workspace,
)


def test_prefix_uses_the_uuid_not_the_name() -> None:
    """Đổi tên workspace KHÔNG được làm đổi vị trí dữ liệu — quyết định của Task 21
    Giai đoạn 1. Prefix dựng từ uuid nên nó không thể phụ thuộc tên."""
    ws = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert prefix_for_workspace(ws) == "workspaces/11111111-2222-3333-4444-555555555555/"


def test_prefix_always_ends_with_a_slash() -> None:
    """Không có dấu / cuối thì prefix `workspaces/<a>` cũng khớp
    `workspaces/<a>-khac/...`. Với một chuỗi dùng làm ranh giới bảo mật, đó là một
    lỗ chứ không phải một chi tiết định dạng."""
    assert prefix_for_workspace(uuid.uuid4()).endswith("/")


def test_lakehouse_prefix_nests_under_the_workspace_prefix() -> None:
    """Bố cục ở spec v1 mục 5.1 có hai tầng, không một. Tầng lakehouse PHẢI nằm
    trong tầng workspace, vì policy STS chỉ giới hạn tới tầng workspace — một
    lakehouse prefix trượt ra ngoài sẽ không đọc nổi, và lỗi hiện ra là 403 chứ
    không phải một câu nói về bố cục."""
    ws = uuid.UUID("11111111-2222-3333-4444-555555555555")
    lh = uuid.UUID("99999999-8888-7777-6666-555555555555")
    assert prefix_for_lakehouse(ws, lh).startswith(prefix_for_workspace(ws))
    assert prefix_for_lakehouse(ws, lh) == (
        "workspaces/11111111-2222-3333-4444-555555555555/"
        "lakehouses/99999999-8888-7777-6666-555555555555/"
    )


def test_expired_credentials_report_themselves_expired() -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    creds = S3Credentials(
        access_key_id="a", secret_access_key="b", session_token="c", expires_at=past
    )
    assert creds.is_expired()


def test_fresh_credentials_are_not_expired() -> None:
    future = datetime.now(UTC) + timedelta(minutes=15)
    creds = S3Credentials(
        access_key_id="a", secret_access_key="b", session_token="c", expires_at=future
    )
    assert not creds.is_expired()


def test_expires_at_must_be_timezone_aware() -> None:
    """Một datetime naive so sánh được với datetime naive khác và im lặng cho ra
    kết quả sai lệch theo múi giờ của máy. `is_expired` so với `now(UTC)`, nên
    naive sẽ ném TypeError lúc chạy — bắt nó ở lúc dựng thì thông báo mới nói
    được nguyên nhân."""
    with pytest.raises(ValueError, match="timezone-aware"):
        S3Credentials(
            access_key_id="a",
            secret_access_key="b",
            session_token="c",
            # datetime naive là ĐIỀU đang kiểm, không phải một sơ suất.
            expires_at=datetime(2030, 1, 1),
        )
