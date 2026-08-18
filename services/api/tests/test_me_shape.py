"""Hình dạng của `GET /api/v1/me` — và cái giá mà nó KHÔNG được trả.

Hai phép canh, hai điều khác nhau:

1. `user_id` có mặt. Không có nó, giao diện không bật được lịch:
   `ScheduleDefinition` bắt buộc `run_as_user_id` là uuid của bảng `user`, và
   `/me` là chỗ duy nhất UI biết id của chính người đang đăng nhập.
2. Handler KHÔNG nhận một `AsyncSession`. `/me` được gọi mỗi lần tải trang, nên
   một round trip thêm ở đây là một round trip cho MỌI trang.

Phép canh (2) đọc CHỮ KÝ HÀM, không đếm câu lệnh SQL — và đó là một quyết định
có lý do. Fixture `api_world` dựng app bằng `Database(db_engine.url...)`, tức app
mở một engine MỚI; listener của fixture `sql_log` gắn trên engine của TEST. Một
`assert sql_log[mark:] == []` quanh một request đi qua `api_world.client` vì vậy
XANH kể cả khi handler truy vấn database — nó rỗng nghĩa. Chữ ký hàm thì không:
đường duy nhất để handler này chạm database là nhận một session qua dependency.
"""

from __future__ import annotations

import inspect
import uuid

from loom_api.routers.auth import me
from loom_core.schemas import CurrentUser, Principal


def test_current_user_carries_user_id() -> None:
    fields = CurrentUser.model_fields
    assert "user_id" in fields, (
        "CurrentUser phải mang user_id — không có nó thì UI không đặt được run_as_user_id"
    )
    assert fields["user_id"].annotation is uuid.UUID


async def test_me_returns_the_principals_user_id() -> None:
    user_id = uuid.uuid4()
    principal = Principal(
        user_id=user_id,
        subject="alice",
        email="alice@loom.local",
        display_name="Alice",
        groups=("authors",),
    )

    result = await me(principal=principal)

    assert result.user_id == user_id
    # Các trường cũ KHÔNG được mất khi thêm trường mới.
    assert result.subject == "alice"
    assert result.email == "alice@loom.local"
    assert result.display_name == "Alice"
    assert result.groups == ("authors",)


def test_me_does_not_take_a_database_session() -> None:
    """`/me` gọi mỗi lần tải trang — một session ở đây là một round trip cho mọi trang.

    Xem docstring module về việc vì sao phép canh này đọc chữ ký chứ không đếm SQL.
    """
    signature = inspect.signature(me)
    names = set(signature.parameters)
    assert "session" not in names, (
        f"`me` nhận tham số {sorted(names)} — một session ở đây là một truy vấn "
        "database cho MỌI lần tải trang"
    )
    annotations = [str(p.annotation) for p in signature.parameters.values()]
    assert not any("Session" in a for a in annotations), (
        f"`me` nhận một kiểu session: {annotations}"
    )
