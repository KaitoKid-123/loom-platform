"""Fixture dùng chung cho test KHÔNG cần Docker.

`tests/integration/conftest.py` (CẦN Docker: MinIO + Postgres + Lakekeeper
thật) kế thừa mọi fixture ở đây — pytest tự nối conftest theo cây thư mục,
`fake_authz`/`a_principal` dùng được ở cả hai nơi mà không cần import.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from loom_core.internal_auth import QUERY_SHARED_SECRET_HEADER
from loom_core.schemas import Principal
from loom_query.authz import AuthzPort, LakehouseResolver
from loom_query.config import Settings


class FakeAuthz:
    """`AuthzPort` VÀ `LakehouseResolver` giả trong CÙNG một đối tượng — đúng
    cách `AuthzClient` thật cài cả hai Protocol trên một `base_url` (xem
    docstring `authz.py`): test không cần tiêm hai fake cho một thứ về bản
    chất là "hỏi loom-api".

    Trả đúng vai trò mà test cấp trước bằng `grant()`, đúng id mà test đăng ký
    trước bằng `register_lakehouse()`, và ghi lại MỌI lần bị gọi vào `calls`/
    `resolve_calls`.

    `calls` là thứ làm "chứng minh đỏ 1" (gỡ bước gọi `/internal/authz/items`
    khỏi `run_gate`) kiểm được: nếu ai đó xoá dòng `await authz.
    roles_for_items(...)` trong `authz.run_gate`, `test_missing_role_is_
    forbidden` ở `test_query_authz_gate.py` sẽ không còn thấy `QueryForbidden`
    nào được ném — nó ĐỎ đúng vì thiếu bước gọi, không phải vì một lý do khác.

    `resolve_calls` cùng vai trò đó cho `resolver.resolve_lakehouses`: một
    phép kiểm join hai lakehouse gọi nó ĐÚNG MỘT lần cho toàn bộ danh sách tên,
    không phải một lần cho mỗi bảng ba phần.
    """

    def __init__(self) -> None:
        self._roles: dict[uuid.UUID, str | None] = {}
        self._lakehouses: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self.calls: list[tuple[uuid.UUID, ...]] = []
        self.resolve_calls: list[tuple[uuid.UUID, tuple[str, ...]]] = []

    def grant(self, item_id: uuid.UUID, role: str) -> None:
        self._roles[item_id] = role

    def register_lakehouse(self, workspace_id: uuid.UUID, name: str, item_id: uuid.UUID) -> None:
        """Đăng ký "trong `workspace_id`, tên `name` là item `item_id`" — bản
        giả của một hàng `Item(type='lakehouse')` mà `/internal/lakehouses/
        resolve` thật sẽ tìm thấy. KHÔNG đăng ký gì cho một (workspace, tên)
        mô phỏng đúng "resolver thật trả None" (tên không tồn tại, hoặc tồn
        tại nhưng ở workspace khác — resolver thật giới hạn theo workspace)."""
        self._lakehouses[(workspace_id, name)] = item_id

    async def roles_for_items(
        self, principal: Principal, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[str, str | None]:
        self.calls.append(item_ids)
        return {str(item_id): self._roles.get(item_id) for item_id in item_ids}

    async def resolve_lakehouses(
        self, workspace_id: uuid.UUID, names: tuple[str, ...]
    ) -> dict[str, uuid.UUID | None]:
        self.resolve_calls.append((workspace_id, names))
        return {name: self._lakehouses.get((workspace_id, name)) for name in names}


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


# Giá trị mặc định của `Settings.shared_secret` khi `environment="local"` —
# mặc định của mọi test dưới đây, không test nào tự đặt `LOOM_QUERY_
# ENVIRONMENT`. Đọc lại từ `Settings()` thay vì chép chuỗi tay: nếu giá trị
# mặc định đó đổi, hằng số này đổi theo mà không ai phải nhớ sửa hai chỗ.
DEFAULT_TEST_SHARED_SECRET = Settings().shared_secret


def http_client(app: FastAPI) -> AsyncClient:
    """`AsyncClient` sẵn header bí mật chia sẻ mà MỌI route của `loom-query`
    giờ đòi hỏi (Task 10/11, xem `security.require_shared_secret`).

    Dùng hàm này thay vì tự dựng `AsyncClient(transport=ASGITransport(app=app),
    ...)` ở từng test: một lần đổi tên header hay giá trị mặc định của
    `shared_secret` thì chỉ có MỘT chỗ phải sửa, không phải mọi file test.
    `tests/test_query_shared_secret.py` là nơi kiểm CHÍNH bản thân phép kiểm
    header — test khác chỉ cần "vượt qua" nó để kiểm đúng thứ chúng đang kiểm.
    """
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={QUERY_SHARED_SECRET_HEADER: DEFAULT_TEST_SHARED_SECRET},
    )


# Xác nhận tĩnh (đọc lúc import module test, không phải một test riêng) rằng
# `FakeAuthz` thật sự khớp CẢ HAI Protocol — một protocol chỉ khớp CẤU TRÚC, và
# một lần đổi chữ ký ở một trong hai bên mà không đổi bên kia sẽ không đỏ ở đâu
# cả nếu không có hai dòng này: mypy strict kiểm nó lúc lint, không phải lúc
# `pytest` chạy hàm nào.
_authz_check: type[AuthzPort] = FakeAuthz
_resolver_check: type[LakehouseResolver] = FakeAuthz
