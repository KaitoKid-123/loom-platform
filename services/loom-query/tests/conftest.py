"""Fixture dùng chung cho test KHÔNG cần Docker.

`tests/integration/conftest.py` (CẦN Docker: MinIO + Postgres + Lakekeeper
thật) kế thừa mọi fixture ở đây — pytest tự nối conftest theo cây thư mục,
`fake_authz`/`a_principal` dùng được ở cả hai nơi mà không cần import.
"""

from __future__ import annotations

import uuid

import pytest

from loom_core.schemas import Principal
from loom_query.authz import AuthzPort


class FakeAuthz:
    """`AuthzPort` giả — trả đúng vai trò mà test cấp trước bằng `grant()`, và
    ghi lại MỌI lần bị gọi vào `calls`.

    `calls` là thứ làm "chứng minh đỏ 1" (gỡ bước gọi `/internal/authz/items`
    khỏi `run_gate`) kiểm được: nếu ai đó xoá dòng `await authz.
    roles_for_items(...)` trong `authz.run_gate`, `test_missing_role_is_
    forbidden` ở `test_query_authz_gate.py` sẽ không còn thấy `QueryForbidden`
    nào được ném — nó ĐỎ đúng vì thiếu bước gọi, không phải vì một lý do khác.
    """

    def __init__(self) -> None:
        self._roles: dict[uuid.UUID, str | None] = {}
        self.calls: list[tuple[uuid.UUID, ...]] = []

    def grant(self, item_id: uuid.UUID, role: str) -> None:
        self._roles[item_id] = role

    async def roles_for_items(
        self, principal: Principal, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[str, str | None]:
        self.calls.append(item_ids)
        return {str(item_id): self._roles.get(item_id) for item_id in item_ids}


def a_principal(*, user_id: uuid.UUID | None = None, groups: tuple[str, ...] = ()) -> Principal:
    return Principal(
        user_id=user_id or uuid.uuid4(),
        subject="test-user",
        email="test-user@loom.local",
        display_name="Test User",
        groups=groups,
    )


@pytest.fixture
def fake_authz() -> FakeAuthz:
    return FakeAuthz()


@pytest.fixture
def principal() -> Principal:
    return a_principal()


# Xác nhận tĩnh (đọc lúc import module test, không phải một test riêng) rằng
# `FakeAuthz` thật sự khớp `AuthzPort` — một protocol chỉ khớp CẤU TRÚC, và một
# lần đổi chữ ký ở một trong hai bên mà không đổi bên kia sẽ không đỏ ở đâu cả
# nếu không có dòng này: mypy strict kiểm nó lúc lint, không phải lúc `pytest`
# chạy hàm nào.
_: type[AuthzPort] = FakeAuthz
