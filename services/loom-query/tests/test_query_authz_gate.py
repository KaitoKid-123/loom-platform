"""Cổng quyền của `loom_query.authz.run_gate` — thứ tự BẮT BUỘC, xem module
docstring của `authz.py`.

Nhiều phép kiểm đóng vai "chứng minh đỏ" của Task 6/7, và cách chứng minh
chúng thật sự đỏ là SỬA `authz.py` theo đúng cách bị cấm rồi chạy lại file
test này:

  - `test_missing_role_on_any_table_is_forbidden` ĐỎ nếu bỏ dòng
    `await authz.roles_for_items(...)` (chứng minh đỏ 1 của Task 6 — gỡ bước
    hỏi quyền).
  - `test_invalid_sql_is_rejected_before_authz_is_ever_called` ĐỎ nếu bỏ lệnh
    gọi `validate(...)` đầu `run_gate` (chứng minh đỏ 3 của Task 6 — gỡ bước
    validate): không có `validate`, `table_deps` tự ném `sqlglot.errors.
    ParseError` — một kiểu lỗi KHÁC `SqlSyntaxError`, không có dòng/cột — nên
    `pytest.raises(SqlSyntaxError)` không còn khớp.
  - `test_every_table_is_checked_not_just_the_first` VÀ
    `test_join_across_two_lakehouses_is_forbidden_missing_the_second_permission`
    ĐỎ nếu đổi cổng quyền từ "mọi bảng" thành "bảng đầu tiên" (chứng minh đỏ 2
    của Task 6, và chứng minh đỏ 1 của Task 7 — chạy lại đúng với dữ liệu
    hai-lakehouse). Bài thứ hai CỐ Ý cấp quyền cho bảng ĐẦU TIÊN (theo thứ tự
    `dependencies()` trả về) và THIẾU quyền ở bảng THỨ HAI — dựng ngược lại
    (thiếu quyền ở bảng đầu) sẽ khiến một bản cài chỉ kiểm "bảng đầu tiên" vẫn
    vô tình ĐỎ đúng vì lý do khác (bảng đầu thiếu quyền), không phải vì lỗi
    "bỏ sót bảng thứ hai" mà phép kiểm này nhắm tới.
  - `test_unresolved_lakehouse_name_is_indistinguishable_from_missing_permission`
    ĐỎ nếu một bản cài dùng một lỗi 404 riêng (hoặc bất kỳ phản hồi nào khác
    `QueryForbidden`) cho "tên lakehouse không phân giải được" (chứng minh đỏ 5
    của Task 7).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from loom_core.schemas import Principal
from loom_query.authz import (
    ExternalSourceRejected,
    QueryForbidden,
    SqlSyntaxError,
    UnsupportedTableName,
    run_gate,
)

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
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert [(r.namespace, r.name) for r in refs] == [("ns", "orders")]
    # Hỏi ĐÚNG MỘT id — chính `lakehouse_id` — không phải id nào khác bịa ra.
    assert fake_authz.calls == [(lakehouse_id,)]
    # Bảng hai phần không có gì để dịch tên — không được gọi resolver.
    assert fake_authz.resolve_calls == []


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
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert fake_authz.calls == [(lakehouse_id,)]


async def test_unqualified_table_name_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    with pytest.raises(UnsupportedTableName):
        await run_gate(
            sql="SELECT * FROM orders",
            lakehouse_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


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
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
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
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )


async def test_invalid_sql_is_rejected_before_authz_is_ever_called(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 3 (gỡ bước `validate`): xem docstring module."""
    with pytest.raises(SqlSyntaxError) as exc_info:
        await run_gate(
            sql="SELECT 1\nFROM foo\nWHERE ((( )",  # hỏng ở dòng 3, xem sqlkit
            lakehouse_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    errors = detail["errors"]
    assert errors, "một lỗi cú pháp phải kèm ít nhất một phần tử errors[]"
    assert errors[0]["line"] == 3
    assert errors[0]["column"] >= 1
    # authz KHÔNG được hỏi cho một câu SQL còn chưa parse xong.
    assert fake_authz.calls == []


async def test_a_query_reading_outside_the_catalog_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """`read_parquet('s3://…')` đọc dữ liệu KHÔNG qua catalog, nên không có item
    nào để hỏi quyền — và nếu để nó đi tiếp thì nó lặng lẽ không bị kiểm gì.

    Chặn ở đây là lớp thứ hai. Lớp thứ nhất là phạm vi credential do Lakekeeper
    cấp; một ranh giới duy nhất không có lớp dự phòng là chỗ một lỗi cấu hình
    biến thành rò rỉ dữ liệu chéo workspace.
    """
    with pytest.raises(ExternalSourceRejected):
        await run_gate(
            sql="SELECT * FROM read_parquet('s3://workspace-khac/bi-mat.parquet')",
            lakehouse_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_the_rejection_happens_before_permissions_are_ever_asked(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Thứ tự quan trọng: hỏi quyền cho một nguồn ngoài catalog là hỏi về một
    item không tồn tại, và câu trả lời `None` trông y hệt "không có quyền" —
    thông báo lỗi sẽ nói sai nguyên nhân cho người dùng."""
    with pytest.raises(ExternalSourceRejected):
        await run_gate(
            sql="SELECT * FROM 's3://workspace-khac/**/*.parquet'",
            lakehouse_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )
    assert fake_authz.calls == [], "không được hỏi quyền cho một thứ không phải item"


async def test_an_ordinary_query_still_passes_the_external_check(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Vế KHẲNG ĐỊNH. Không có nó, một bản cài từ chối MỌI query cũng làm hai
    phép trên xanh — và lúc đó không ai chạy được gì."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    refs = await run_gate(
        sql="SELECT * FROM sales.orders",
        lakehouse_id=lakehouse_id,
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )
    assert [r.name for r in refs] == ["orders"]


# --------------------------------------------------------- Task 7: hai lakehouse


async def test_three_part_table_resolves_the_named_lakehouse_within_the_workspace(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Tên BA phần (`lakehouse.namespace.table`) giờ được hỗ trợ: `lakehouse`
    là `name` của một item `lakehouse` KHÁC, cùng workspace — không còn bị từ
    chối 400 "chưa hỗ trợ" như bản cũ (Task 6)."""
    workspace_id = uuid.uuid4()
    other_lakehouse_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_lakehouse_id)
    fake_authz.grant(other_lakehouse_id, "viewer")

    refs = await run_gate(
        sql="SELECT * FROM other.ns.orders",
        lakehouse_id=uuid.uuid4(),  # KHÔNG dùng tới — bảng chỉ trỏ tới "other"
        workspace_id=workspace_id,
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert [(r.namespace, r.name) for r in refs] == [("other.ns", "orders")]
    # Hỏi quyền trên ID ĐÃ PHÂN GIẢI, không phải trên `lakehouse_id` của request.
    assert fake_authz.calls == [(other_lakehouse_id,)]
    assert fake_authz.resolve_calls == [(workspace_id, ("other",))]


async def test_join_across_two_lakehouses_runs_when_both_are_permitted(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_id)
    fake_authz.grant(lakehouse_id, "viewer")
    fake_authz.grant(other_id, "viewer")

    refs = await run_gate(
        sql="SELECT * FROM aaa.orders o JOIN other.finance.reports r ON o.id = r.id",
        lakehouse_id=lakehouse_id,
        workspace_id=workspace_id,
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert {(r.namespace, r.name) for r in refs} == {
        ("aaa", "orders"),
        ("other.finance", "reports"),
    }
    assert set(fake_authz.calls[0]) == {lakehouse_id, other_id}


async def test_join_across_two_lakehouses_is_forbidden_missing_the_second_permission(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 1 của Task 7 (== chứng minh đỏ 2 của Task 6, chạy lại với
    dữ liệu hai-lakehouse): xem docstring module cho lý do bảng ĐẦU TIÊN
    (`aaa.orders`, sắp trước `other.finance.reports` theo thứ tự
    `dependencies()` trả về — đã kiểm bằng thực nghiệm sqlglot) được cấp
    quyền còn bảng THỨ HAI thì KHÔNG: một bản cài chỉ kiểm bảng đầu tiên sẽ
    thấy `lakehouse_id` có quyền và cho qua — SAI, vì `other_id` (bảng thứ
    hai) không hề được cấp gì."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_id)
    fake_authz.grant(lakehouse_id, "viewer")  # CHỈ bảng đầu tiên có quyền

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM aaa.orders o JOIN other.finance.reports r ON o.id = r.id",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    assert set(fake_authz.calls[0]) == {lakehouse_id, other_id}


async def test_join_across_two_lakehouses_is_forbidden_missing_both_permissions(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_id)
    # KHÔNG grant gì cho lakehouse nào cả.

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM aaa.orders o JOIN other.finance.reports r ON o.id = r.id",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_join_where_the_second_lakehouse_name_does_not_resolve_is_forbidden(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Không `register_lakehouse` cho "khong-ton-tai" — resolver trả `None`
    cho nó, đúng hành vi khi tên không tồn tại HOẶC ở workspace khác. Câu trả
    lời vẫn phải là 403, không phải một mã lỗi riêng."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM aaa.orders o JOIN khong_ton_tai.ns.t r ON o.id = r.id",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    # Tên không phân giải được -> KHÔNG có id nào để hỏi `/internal/authz/items`
    # thay cho nó — `run_gate` chặn ngay ở bước phân giải.
    assert fake_authz.calls == []


async def test_unresolved_lakehouse_name_is_indistinguishable_from_missing_permission(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 5 của Task 7: nếu bản cài dùng một lỗi 404 riêng (hay bất
    kỳ hình dạng phản hồi nào khác) cho "tên không phân giải được" thay vì tái
    dùng CHÍNH `QueryForbidden`, một trong hai câu khẳng định dưới đây đỏ.
    Hai kịch bản — tên hoàn toàn không tồn tại, và tên tồn tại nhưng principal
    không có quyền — phải sinh ra hai exception Y HỆT NHAU, không chỉ "cùng mã
    trạng thái"."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    with pytest.raises(QueryForbidden) as unresolved:
        await run_gate(
            sql="SELECT * FROM aaa.orders o JOIN khong_ton_tai.ns.t r ON o.id = r.id",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "no_access", other_id)
    # `other_id` TỒN TẠI (đăng ký được) nhưng KHÔNG được `grant` — có thật
    # nhưng principal không có quyền đọc.
    with pytest.raises(QueryForbidden) as no_permission:
        await run_gate(
            sql="SELECT * FROM aaa.orders o JOIN no_access.ns.t r ON o.id = r.id",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    assert unresolved.value.status_code == status.HTTP_403_FORBIDDEN
    assert unresolved.value.status_code == no_permission.value.status_code
    assert unresolved.value.detail == no_permission.value.detail


async def test_a_lakehouse_name_from_a_different_workspace_does_not_resolve(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Cộng thêm của Task 7: lakehouse THẬT SỰ tồn tại, nhưng đăng ký dưới một
    `workspace_id` KHÁC — `run_gate` phải truyền đúng `workspace_id` của
    request cho `resolver`, không phải một workspace nào khác, nên tên đó
    không phân giải được TỪ request này và kết quả là 403 (không phải một mã
    lỗi khác, và không phải chạy được).

    Đây là phép kiểm ở tầng HỢP ĐỒNG (`run_gate` truyền đúng tham số cho
    `resolver`); tính đúng đắn của chính câu SQL lọc `workspace_id` bên
    `loom-api` có phép kiểm riêng, xem
    `services/api/tests/integration/test_lakehouses_resolve.py`.
    """
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_b, "other", other_id)  # workspace KHÁC
    fake_authz.grant(lakehouse_id, "viewer")
    fake_authz.grant(other_id, "viewer")  # có quyền, nhưng không tìm thấy được

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM aaa.orders o JOIN other.ns.t r ON o.id = r.id",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_a,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_cross_lakehouse_names_are_resolved_in_a_single_call_not_per_table(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Một `JOIN` chạm HAI bảng ở CÙNG một lakehouse thứ hai phải sinh MỘT lần
    gọi `resolve_lakehouses` cho cả hai tên, không phải hai lần — N+1 ở đây là
    N+1 round trip HTTP tới `loom-api`."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_id)
    fake_authz.grant(lakehouse_id, "viewer")
    fake_authz.grant(other_id, "viewer")

    await run_gate(
        sql=(
            "SELECT * FROM aaa.orders o "
            "JOIN other.ns.t1 t1 ON o.id = t1.id "
            "JOIN other.ns.t2 t2 ON o.id = t2.id"
        ),
        lakehouse_id=lakehouse_id,
        workspace_id=workspace_id,
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert len(fake_authz.resolve_calls) == 1
    assert fake_authz.resolve_calls[0] == (workspace_id, ("other",))
