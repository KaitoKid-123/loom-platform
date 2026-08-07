"""`loom_query.lakehouse_schema.run_schema_gate` — cổng quyền của
`GET /api/v1/lakehouses/{lakehouse_id}/schema` (Task 2, Giai đoạn 2c).

`test_missing_role_is_forbidden` là chứng minh đỏ 1 (BẮT BUỘC theo spec): gỡ
dòng `await authz.roles_for_items(...)` khỏi `run_schema_gate` sẽ làm bài này
ĐỎ — không có `SchemaForbidden` nào được ném, và `fake_authz.calls` rỗng thay
vì mang đúng `(lakehouse_id,)`. Đã xác nhận bằng thực nghiệm (comment dòng đó
ra rồi chạy lại file test này): `pytest.raises(SchemaForbidden)` không còn
khớp, đúng tín hiệu đỏ SẠCH — không bị một exception nào khác che mất, và
`AttributeError`/`NameError` không xảy ra vì `roles` không còn được dùng ở đâu
khác.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from loom_core.schemas import Principal
from loom_query.lakehouse_schema import SchemaForbidden, run_schema_gate

from .conftest import FakeAuthz

# `fake_authz`/`principal` là fixture của `tests/conftest.py`, tiêm thẳng theo
# tên tham số — không cần import. `FakeAuthz` ở trên chỉ để chú thích kiểu.


async def test_missing_role_is_forbidden(fake_authz: FakeAuthz, principal: Principal) -> None:
    lakehouse_id = uuid.uuid4()

    with pytest.raises(SchemaForbidden):
        await run_schema_gate(lakehouse_id=lakehouse_id, principal=principal, authz=fake_authz)

    # Bằng chứng cổng THẬT SỰ đã hỏi — không phải trùng hợp bị chặn vì lý do
    # khác. Xem docstring module cho lý do đây là chứng minh đỏ 1.
    assert fake_authz.calls == [(lakehouse_id,)]


async def test_a_viewer_role_is_sufficient(fake_authz: FakeAuthz, principal: Principal) -> None:
    """`viewer` là vai trò THẤP NHẤT có `item.read` — nó phải đủ, không cần gì
    cao hơn (cùng lý do `authz.py::test_a_viewer_role_is_sufficient` tồn tại
    cho `/query`)."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    await run_schema_gate(lakehouse_id=lakehouse_id, principal=principal, authz=fake_authz)


async def test_a_higher_role_is_also_sufficient(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "admin")

    await run_schema_gate(lakehouse_id=lakehouse_id, principal=principal, authz=fake_authz)


async def test_the_forbidden_response_never_says_why(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Cố ý KHÔNG kiểm được lý do bên trong `roles.get(...) is None` — ĐÓ mới
    chính là điểm: `run_schema_gate` không có khái niệm "id không tồn tại"
    tách khỏi "thiếu quyền" (xem module docstring `lakehouse_schema.py`), nên
    hai id KHÔNG BAO GIỜ được `grant()` gì (một trong hai CÓ THỂ "không tồn
    tại", cái còn lại CÓ THỂ "tồn tại nhưng không có quyền" — `run_schema_gate`
    không có cách nào biết, và không được phép biết) phải cho ra CÙNG một
    exception, cùng status, cùng thông điệp."""
    with pytest.raises(SchemaForbidden) as first:
        await run_schema_gate(lakehouse_id=uuid.uuid4(), principal=principal, authz=fake_authz)
    with pytest.raises(SchemaForbidden) as second:
        await run_schema_gate(lakehouse_id=uuid.uuid4(), principal=principal, authz=fake_authz)

    assert first.value.status_code == status.HTTP_403_FORBIDDEN
    assert first.value.status_code == second.value.status_code
    assert first.value.detail == second.value.detail
