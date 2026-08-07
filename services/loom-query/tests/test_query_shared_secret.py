"""Bí mật chia sẻ qua header — Task 10/11, xem `loom_query.security`.

Ba "chứng minh đỏ" bắt buộc của Phần B (spec Giai đoạn 2b):

1. Gọi thẳng `loom-query` KHÔNG kèm header → 401
   (`test_missing_header_is_rejected`).
2. Header mang giá trị SAI → 401, không phải 500 hay 403
   (`test_wrong_secret_is_rejected_with_401_not_500_or_403`).
3. Gỡ `security.require_shared_secret` khỏi `dependencies=` của router →
   cả hai test trên phải ĐỎ (chuyển từ 401 sang 403/202, tuỳ request) —
   `test_correct_secret_passes_the_gate` là chốt chống-xanh-rỗng đi kèm: nếu
   MỌI request đều 401 bất kể header (ví dụ do lỗi ở một tầng khác), hai test
   ở trên xanh vì lý do sai, và test này bắt được điều đó.

Dùng `create_app()` với `authz=fake_authz` NHƯNG cố tình không `grant()` gì —
nếu phép kiểm header lỡ chạy SAU cổng quyền thay vì TRƯỚC nó (một race tái
cấu trúc hoàn toàn có thể xảy ra), request thiếu header vẫn phải 401 chứ
không phải 403 — cách duy nhất phân biệt được thứ tự đó từ bên ngoài.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from loom_core.internal_auth import QUERY_SHARED_SECRET_HEADER
from loom_core.schemas import Principal
from loom_query.config import Settings
from loom_query.main import create_app

from .conftest import DEFAULT_TEST_SHARED_SECRET, FakeAuthz, http_client


def _body(lakehouse_id: uuid.UUID, principal: Principal) -> dict[str, object]:
    return {
        "lakehouse_id": str(lakehouse_id),
        "workspace_id": str(uuid.uuid4()),
        "sql": "SELECT * FROM ns.orders",
        "principal": {
            "user_id": str(principal.user_id),
            "subject": principal.subject,
            "email": principal.email,
            "display_name": principal.display_name,
            "groups": list(principal.groups),
        },
    }


async def test_missing_header_is_rejected(fake_authz: FakeAuthz, principal: Principal) -> None:
    app = create_app(authz=fake_authz)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/query", json=_body(uuid.uuid4(), principal))
    assert response.status_code == 401


async def test_wrong_secret_is_rejected_with_401_not_500_or_403(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """SAI bí mật phải là 401 sạch — không phải 500 (một so sánh ném ngoại lệ)
    và không phải 403 (thứ ngụ ý danh tính đã được chấp nhận nhưng thiếu
    quyền — sai hoàn toàn về BẢN CHẤT của lỗi này)."""
    app = create_app(authz=fake_authz)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/query",
            json=_body(uuid.uuid4(), principal),
            headers={QUERY_SHARED_SECRET_HEADER: "khong-phai-bi-mat-that"},
        )
    assert response.status_code == 401


async def test_correct_secret_passes_the_gate(fake_authz: FakeAuthz, principal: Principal) -> None:
    """Chốt chống-xanh-rỗng cho hai test trên: nếu header ĐÚNG mà vẫn 401 thì
    cả hai phép kiểm phía trên xanh vì một lý do khác hẳn (mọi request đều
    401 bất kể header), không phải vì chúng thật sự phân biệt đúng/sai/thiếu.

    Không `grant()` lakehouse này nên 403 (thiếu viewer) — KHÔNG PHẢI 401 —
    mới là câu trả lời đúng khi header hợp lệ."""
    app = create_app(authz=fake_authz)
    async with http_client(app) as client:
        response = await client.post("/api/v1/query", json=_body(uuid.uuid4(), principal))
    assert response.status_code == 403


async def test_get_and_delete_are_gated_too(fake_authz: FakeAuthz) -> None:
    """`GET`/`DELETE` đứng sau CÙNG một `dependencies=` ở cấp router — không
    phải một phép kiểm riêng dán vào từng handler mà `POST` có thể có còn hai
    route kia thì quên."""
    app = create_app(authz=fake_authz)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_response = await client.get(f"/api/v1/query/{uuid.uuid4()}")
        delete_response = await client.delete(f"/api/v1/query/{uuid.uuid4()}")
    assert get_response.status_code == 401
    assert delete_response.status_code == 401


def test_the_insecure_default_is_rejected_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cùng khuôn `packages/core/tests/test_config.py::
    test_default_secrets_rejected_outside_local` — bản sao CỦA `loom-query`
    (xem comment `_INSECURE_DEFAULTS` ở `loom_query.config`)."""
    monkeypatch.setenv("LOOM_QUERY_ENVIRONMENT", "prod")
    with pytest.raises(ValidationError, match="shared_secret"):
        Settings()


def test_a_real_value_is_accepted_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_QUERY_ENVIRONMENT", "prod")
    monkeypatch.setenv("LOOM_QUERY_SHARED_SECRET", "a-real-secret-value")
    settings = Settings()
    assert settings.shared_secret == "a-real-secret-value"


def test_default_is_allowed_in_local() -> None:
    settings = Settings(environment="local")
    assert settings.shared_secret == DEFAULT_TEST_SHARED_SECRET
