"""Cổng quyền của `loom_query.authz.run_gate` — thứ tự BẮT BUỘC, xem module
docstring của `authz.py`.

Ba phép kiểm đóng vai "chứng minh đỏ" của Task 6, và cách chứng minh chúng
thật sự đỏ là SỬA `authz.py` theo đúng cách bị cấm rồi chạy lại file test này:

  - `test_missing_role_on_any_table_is_forbidden` ĐỎ nếu bỏ dòng
    `await authz.roles_for_items(...)` (chứng minh đỏ 1 — gỡ bước hỏi quyền).
  - `test_invalid_sql_is_rejected_before_authz_is_ever_called` ĐỎ nếu bỏ lệnh
    gọi `validate(...)` đầu `run_gate` (chứng minh đỏ 3 — gỡ bước validate):
    không có `validate`, `table_deps` tự ném `sqlglot.errors.ParseError` — một
    kiểu lỗi KHÁC `SqlSyntaxError`, không có dòng/cột — nên
    `pytest.raises(SqlSyntaxError)` không còn khớp.
  - `test_two_part_table_resolves_to_the_lakehouse_of_the_request` ĐỎ nếu đổi
    cổng quyền từ "mọi bảng" thành "bảng đầu tiên" (chứng minh đỏ 2 của Task
    6/7, chỉ thật sự lộ rõ khi có hai bảng — xem test đó).
"""

from __future__ import annotations

import uuid

import pytest

from loom_core.schemas import Principal
from loom_query.authz import QueryForbidden, SqlSyntaxError, UnsupportedTableName, run_gate

from .conftest import FakeAuthz

# `fake_authz`/`principal` là fixture của `tests/conftest.py`, tiêm thẳng theo
# tên tham số — không cần import. `FakeAuthz` ở trên chỉ để chú thích kiểu.


async def test_two_part_table_resolves_to_the_lakehouse_of_the_request(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    refs = await run_gate(
        sql="SELECT * FROM ns.orders",
        lakehouse_id=lakehouse_id,
        principal=principal,
        authz=fake_authz,
    )

    assert [(r.namespace, r.name) for r in refs] == [("ns", "orders")]
    # Hỏi ĐÚNG MỘT id — chính `lakehouse_id` — không phải id nào khác bịa ra.
    assert fake_authz.calls == [(lakehouse_id,)]


async def test_every_table_is_checked_not_just_the_first(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Hai bảng, hai namespace, CÙNG một lakehouse: cả hai phải vào danh sách
    được hỏi quyền. Đổi cài đặt để chỉ kiểm `refs[0]` (đúng lỗi mà spec Task 6
    gọi là "chứng minh đỏ 2") sẽ làm `fake_authz.calls` chỉ còn MỘT id thay vì
    hai, và câu khẳng định dưới đây đỏ."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    await run_gate(
        sql="SELECT * FROM a.t1 JOIN b.t2 ON a.t1.id = b.t2.id",
        lakehouse_id=lakehouse_id,
        principal=principal,
        authz=fake_authz,
    )

    assert fake_authz.calls == [(lakehouse_id,)]


async def test_unqualified_table_name_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    with pytest.raises(UnsupportedTableName):
        await run_gate(
            sql="SELECT * FROM orders",
            lakehouse_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
        )


async def test_three_part_table_name_says_not_supported_yet(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    with pytest.raises(UnsupportedTableName) as exc_info:
        await run_gate(
            sql="SELECT * FROM other_lakehouse.ns.orders",
            lakehouse_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
        )
    assert "not supported yet" in str(exc_info.value.detail)


async def test_missing_role_on_any_table_is_forbidden(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 1 (gỡ bước gọi `/internal/authz/items`): xem docstring
    module. Không `grant` gì, nên `fake_authz` trả `None` cho mọi id — phải
    ra 403, và `calls` phải cho thấy nó THẬT SỰ đã hỏi."""
    lakehouse_id = uuid.uuid4()

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM ns.orders",
            lakehouse_id=lakehouse_id,
            principal=principal,
            authz=fake_authz,
        )

    assert fake_authz.calls == [(lakehouse_id,)]


async def test_a_viewer_role_is_sufficient(fake_authz: FakeAuthz, principal: Principal) -> None:
    """`viewer` là vai trò THẤP NHẤT có `item.read` — nó phải đủ, không cần gì
    cao hơn. (Không có test này thì một cài đặt lỡ đòi `contributor` trở lên
    vẫn xanh ở mọi phép kiểm khác.)"""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    await run_gate(
        sql="SELECT * FROM ns.orders",
        lakehouse_id=lakehouse_id,
        principal=principal,
        authz=fake_authz,
    )


async def test_invalid_sql_is_rejected_before_authz_is_ever_called(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 3 (gỡ bước `validate`): xem docstring module."""
    with pytest.raises(SqlSyntaxError) as exc_info:
        await run_gate(
            sql="SELECT 1\nFROM foo\nWHERE ((( )",  # hỏng ở dòng 3, xem sqlkit
            lakehouse_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
        )

    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    errors = detail["errors"]
    assert errors, "một lỗi cú pháp phải kèm ít nhất một phần tử errors[]"
    assert errors[0]["line"] == 3
    assert errors[0]["column"] >= 1
    # authz KHÔNG được hỏi cho một câu SQL còn chưa parse xong.
    assert fake_authz.calls == []
