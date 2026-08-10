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
    InvalidFilesPath,
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

    assert [(t.ref.namespace, t.ref.name) for t in refs] == [("ns", "orders")]
    assert {t.lakehouse_id for t in refs} == {lakehouse_id}
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


async def test_a_multi_statement_write_never_reaches_the_permission_check(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Nhiều câu lệnh phải chết ở `validate`, TRƯỚC khi ai đó hỏi quyền.

    Không có phép chặn đó thì đây là một lỗ leo thang quyền, không phải một
    chuyện phong cách. `dependencies()` gọi `parse_one`, mà nhiều câu lệnh cho
    ra một `exp.Block`; `_write_destination` không nhận ra `Block` nên trả
    `None`, và đích GHI bị xếp thành bảng ĐỌC:

        dependencies("SELECT 1; CREATE TABLE ns.t AS SELECT 1")
           -> writes=[]  reads=[ns.t]

    Một `reads` thì `run_gate` chỉ đòi `item_read` — tức VIEWER được phép nộp
    một câu lệnh GHI, trong khi `ACTION_MATRIX` đặt `item.update` ở
    `contributor`. Và DuckDB thì có chạy cả chuỗi câu lệnh.

    Khẳng định `fake_authz.calls == []` mới là phần quan trọng: nó nói câu SQL
    này không hề tới được chỗ quyết định quyền. Chỉ khẳng định "có ném lỗi" thì
    vẫn xanh kể cả khi cây bị đọc sai rồi mới bị từ chối vì một lý do khác.
    """
    with pytest.raises(SqlSyntaxError):
        await run_gate(
            sql="SELECT 1; CREATE TABLE ns.t AS SELECT 1",
            lakehouse_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    assert fake_authz.calls == []


async def test_a_query_reading_outside_the_catalog_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """`read_parquet('s3://…')` đọc dữ liệu KHÔNG qua catalog, nên không có item
    nào để hỏi quyền — và nếu để nó đi tiếp thì nó lặng lẽ không bị kiểm gì.

    Chặn ở đây là lớp thứ hai. Lớp thứ nhất là phạm vi credential do Lakekeeper
    cấp; một ranh giới duy nhất không có lớp dự phòng là chỗ một lỗi cấu hình
    biến thành rò rỉ dữ liệu chéo workspace.

    **Đổi exception ở Task 13:** trước đó bài này khẳng định `ExternalSourceRejected`
    — đúng lúc `read_parquet` CHƯA được phục vụ chút nào. Giờ `read_parquet`/
    `read_csv` LÀ tính năng được hỗ trợ (đọc `Files/` của lakehouse), nên một
    path tuyệt đối/có scheme bị từ chối vì nó KHÔNG AN TOÀN
    (`InvalidFilesPath`, nói rõ lý do), không phải vì `read_parquet` "chưa hỗ
    trợ" (`ExternalSourceRejected`) — hai thông điệp khác nhau cho hai người
    dùng khác nhau: một người gõ sai path, một người gọi một hàm hoàn toàn lạ.
    Vẫn 400, vẫn từ chối TRƯỚC khi chạm S3 — hành vi bên ngoài không đổi, chỉ
    lý do được nói rõ hơn. `test_generate_series_is_still_rejected` bên dưới
    là bài giữ nguyên `ExternalSourceRejected` cho phần KHÔNG được nới.
    """
    with pytest.raises(InvalidFilesPath):
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
    assert [t.ref.name for t in refs] == ["orders"]


# --------------------------------------------------------- Task 7: hai lakehouse


async def test_three_part_table_resolves_the_named_lakehouse_within_the_workspace(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Tên BA phần (`lakehouse.namespace.table`) giờ được hỗ trợ: `lakehouse`
    là `name` của một item `lakehouse` KHÁC, cùng workspace — không còn bị từ
    chối 400 "chưa hỗ trợ" như bản cũ (Task 6).

    `lakehouse_id` của request (KHÔNG được trỏ tới bởi bất kỳ bảng nào trong
    câu SQL — mọi bảng ở đây đều là ba phần, trỏ tới "other") vẫn PHẢI được
    cấp quyền: `runner._run_sync` mở catalog của nó vô điều kiện (đó là catalog
    "nhà" cho MỌI bảng hai phần dù câu SQL này không có bảng nào như vậy), nên
    thiếu quyền trên `lakehouse_id` vẫn phải chặn — xem
    `test_three_part_only_query_still_needs_the_primary_lakehouses_permission`
    cho phép kiểm khẳng định đúng NGƯỢC LẠI của tiền đề này (không cấp quyền
    `lakehouse_id` -> 403)."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_lakehouse_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_lakehouse_id)
    fake_authz.grant(lakehouse_id, "viewer")
    fake_authz.grant(other_lakehouse_id, "viewer")

    refs = await run_gate(
        sql="SELECT * FROM other.ns.orders",
        lakehouse_id=lakehouse_id,
        workspace_id=workspace_id,
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert [(t.ref.namespace, t.ref.name) for t in refs] == [("other.ns", "orders")]
    assert [t.lakehouse_id for t in refs] == [other_lakehouse_id]
    # Hỏi quyền trên CẢ hai: id đã phân giải cho bảng, VÀ `lakehouse_id` của
    # chính request (bất biến bảo mật — xem `run_gate`).
    assert set(fake_authz.calls[0]) == {lakehouse_id, other_lakehouse_id}
    assert fake_authz.resolve_calls == [(workspace_id, ("other",))]


async def test_three_part_only_query_still_needs_the_primary_lakehouses_permission(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ QUAN TRỌNG NHẤT của lần sửa này — lỗ bảo mật đã bị vi
    phạm: một câu SQL toàn tên BA phần (không một bảng hai phần nào trỏ về
    `lakehouse_id` của chính request) vẫn PHẢI hỏi quyền trên `lakehouse_id`
    đó.

    `runner._run_sync` mở catalog Iceberg của `lakehouse_id` VÔ ĐIỀU KIỆN cho
    mọi bảng hai phần tiềm năng — nếu `run_gate` chỉ hỏi quyền trên những
    lakehouse THẬT SỰ được một bảng trỏ tới, một câu SQL toàn-ba-phần trỏ tới
    lakehouse B (người dùng CÓ quyền) trong khi `lakehouse_id=A` của chính
    request KHÔNG có quyền vẫn lọt qua cổng — runner vẫn mở catalog của A một
    cách vô điều kiện dù A chưa hề được cấp quyền.

    Bỏ `lakehouse_id` ra khỏi biểu thức xây `item_ids` trong `run_gate` (quay
    lại bản chỉ đưa lakehouse của bảng hai phần vào) sẽ làm phép kiểm này ĐỎ:
    `other_id` (B) được cấp quyền và là lakehouse DUY NHẤT một bảng nào đó trỏ
    tới, nên `item_ids` sẽ chỉ còn `{other_id}`, `roles_for_items` trả đủ vai
    trò, và `run_gate` sai lầm cho qua."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()  # A — request dùng id này, KHÔNG được cấp quyền
    other_id = uuid.uuid4()  # B — CÓ quyền, và là lakehouse DUY NHẤT được trỏ tới
    fake_authz.register_lakehouse(workspace_id, "b", other_id)
    fake_authz.grant(other_id, "viewer")
    # `lakehouse_id` (A) KHÔNG được `grant` gì — đây chính là tiền đề của lỗ.

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM b.finance.reports",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    # `roles_for_items` PHẢI được hỏi cho CẢ `lakehouse_id` (A) — bằng chứng
    # nó thật sự nằm trong tập được kiểm, không chỉ "bị từ chối vì lý do khác".
    assert set(fake_authz.calls[0]) == {lakehouse_id, other_id}


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

    assert {(t.ref.namespace, t.ref.name) for t in refs} == {
        ("aaa", "orders"),
        ("other.finance", "reports"),
    }
    assert {t.ref.name: t.lakehouse_id for t in refs} == {
        "orders": lakehouse_id,
        "reports": other_id,
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


# --------------------------------------------------------- Task 13: Files/


async def test_read_parquet_under_files_of_the_request_lakehouse_is_allowed(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Vế KHẲNG ĐỊNH của cổng quyền cho Task 13: một `read_parquet('Files/…')`
    hợp lệ, với quyền viewer trên `lakehouse_id`, không bị `run_gate` chặn.
    Không có bài này, ba bài TỪ CHỐI bên dưới cũng xanh với một bản cài từ
    chối MỌI query — xem cảnh báo ở đầu module `authz.py`/báo cáo Task 13."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    refs = await run_gate(
        sql="SELECT * FROM read_parquet('Files/thang-01/a.parquet')",
        lakehouse_id=lakehouse_id,
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    assert refs == ()  # không có bảng catalog nào trong câu này


async def test_a_files_only_query_without_the_lakehouses_permission_is_forbidden(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 2 của Task 13: `read_parquet('Files/…')` không kèm bảng
    catalog nào (`deps.tables == []`) — nếu `run_gate` chỉ hỏi quyền cho bảng
    trong `deps.tables`, KHÔNG có id nào để hỏi, và query lọt qua mà chưa hề
    kiểm quyền viewer trên `lakehouse_id`.

    Bài này KHÔNG grant gì, nên `fake_authz` trả `None` cho mọi id — phải ra
    403, và `calls` phải cho thấy `lakehouse_id` THẬT SỰ đã bị hỏi (bằng chứng
    nó nằm trong tập được kiểm, không phải trùng hợp bị chặn vì lý do khác).

    Đã xác nhận bằng thực nghiệm (xem báo cáo hoàn tất Task 13): tạm đổi dòng
    xây `item_ids` trong `run_gate` từ `(lakehouse_id, *(...))` thành chỉ
    `tuple(t.lakehouse_id for t in resolved_tables)` (bỏ phần đưa `lakehouse_id`
    vô điều kiện — invariant đã sửa ở Task 7) làm bài này ĐỎ: `item_ids` rỗng,
    `roles_for_items` không bị gọi, không có `missing`, và `run_gate` trả về
    `()` thay vì ném `QueryForbidden`.
    """
    lakehouse_id = uuid.uuid4()

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="SELECT * FROM read_parquet('Files/thang-01/a.parquet')",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    assert fake_authz.calls == [(lakehouse_id,)]


async def test_read_parquet_with_an_absolute_path_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """`InvalidFilesPath`, không `ExternalSourceRejected` — xem docstring
    `test_a_query_reading_outside_the_catalog_is_rejected` cho lý do đổi.

    Quyền viewer được CẤP SẴN trên `lakehouse_id` (khác các bài `FakeAuthz()`
    trần trụi khác) — có chủ đích: nếu phép kiểm path bị gỡ, câu SQL này phải
    THÀNH CÔNG hoàn toàn (không exception nào), không bị che bởi một 403 tình
    cờ do thiếu quyền. `pytest.raises(InvalidFilesPath)` khi đó sẽ đỏ vì
    KHÔNG CÓ exception nào được ném, chứ không phải vì một exception SAI loại
    — tín hiệu đỏ rõ ràng, không mập mờ (xem cảnh báo về "chứng minh đỏ bị
    che" trong báo cáo hoàn tất Task 13)."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    with pytest.raises(InvalidFilesPath):
        await run_gate(
            sql="SELECT * FROM read_parquet('/etc/passwd')",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_read_parquet_escaping_the_files_prefix_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 1 của Task 13 — xem `services/loom-query/tests/
    test_files.py::test_escaping_the_files_prefix_with_dotdot_is_rejected`
    cho phép kiểm chi tiết ở tầng `safe_relative_path`; bài này khẳng định
    ĐÚNG cùng lỗ đó bị chặn ở tầng `run_gate` (400 TRƯỚC khi có bất kỳ tác vụ
    nền nào, KHÔNG phải một `failed` sau khi đã trả `202`).

    Quyền viewer CẤP SẴN — cùng lý do đã ghi ở
    `test_read_parquet_with_an_absolute_path_is_rejected`: gỡ phép kiểm path
    phải làm câu SQL này CHẠY ĐƯỢC, không bị một 403 không liên quan che mất
    tín hiệu đỏ."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    with pytest.raises(InvalidFilesPath):
        await run_gate(
            sql="SELECT * FROM read_parquet('Files/../../khac/x.parquet')",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_read_parquet_without_a_literal_path_is_rejected(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    with pytest.raises(InvalidFilesPath):
        await run_gate(
            sql="SELECT * FROM read_parquet(some_column)",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_generate_series_is_still_rejected_not_widened_by_files_support(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 5 của Task 13 (lệnh cấm cũ còn nguyên), phần bền vững:
    `range`/`generate_series` không đọc dữ liệu từ đâu cả — QUYẾT ĐỊNH của
    Task 13 là KHÔNG mở khoá cho chúng (xem báo cáo hoàn tất).

    Quyền viewer CẤP SẴN trên `lakehouse_id` — có chủ đích: câu SQL này không
    có bảng catalog nào (`deps.tables == []`), nên nếu cổng nhãn trong
    `_check_files_access` bị gỡ (nới quá tay, hỏi thẳng `validate_files_paths`
    cho MỌI hàm bảng), `range(10)` sẽ trót lọt qua HOÀN TOÀN (không có path
    nào để `validate_files_paths` từ chối — `file_read_calls` không hề biết
    tới `range`) và `run_gate` trả về `()` thành công thay vì ném
    `ExternalSourceRejected` — một tín hiệu đỏ SẠCH (không exception, không bị
    một 403 không liên quan che mất), đã kiểm bằng thực nghiệm (xem báo cáo
    hoàn tất Task 13)."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    with pytest.raises(ExternalSourceRejected):
        await run_gate(
            sql="SELECT * FROM range(10)",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


# --------------------------------------------------------- Giai đoạn 2c: CTAS


async def test_ctas_requires_contributor_viewer_is_forbidden(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 2 (bắt buộc) của Giai đoạn 2c: GHI đòi `contributor`
    (`item.update`, `loom_core.roles.ACTION_MATRIX`), không chỉ `viewer` như
    ĐỌC. Một principal chỉ có `viewer` chạy CTAS phải bị 403 — hạ yêu cầu
    xuống `viewer` (ví dụ đổi `Action.item_update` thành `Action.item_read`
    cho vế ghi trong `run_gate`) phải làm bài này ĐỎ."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="CREATE TABLE bronze.moi AS SELECT * FROM bronze.nguon",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_ctas_succeeds_with_contributor(fake_authz: FakeAuthz, principal: Principal) -> None:
    """Vế KHẲNG ĐỊNH của bài trên — không có nó, một bản cài từ chối MỌI CTAS
    (kể cả với contributor) cũng làm bài trên xanh mà không chứng minh gì.
    Cũng khoá đúng hình dạng `ResolvedTable` mà `runner` cần: đích (`moi`) là
    GHI, không ĐỌC; nguồn (`nguon`) là ĐỌC, không GHI."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "contributor")

    refs = await run_gate(
        sql="CREATE TABLE bronze.moi AS SELECT * FROM bronze.nguon",
        lakehouse_id=lakehouse_id,
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )

    by_name = {t.ref.name: t for t in refs}
    assert by_name["moi"].is_write is True
    assert by_name["moi"].is_read is False
    assert by_name["nguon"].is_write is False
    assert by_name["nguon"].is_read is True


async def test_a_read_only_query_still_only_requires_viewer_not_contributor(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 3 (bắt buộc): ĐỌC vẫn chỉ đòi `viewer`. Không có bài này,
    một bản cài đòi `contributor` cho MỌI THỨ (kể cả đọc thuần) vẫn làm bài
    `test_ctas_requires_contributor_viewer_is_forbidden` xanh — hai bài phải
    đứng CẠNH nhau để phân biệt được hai cách hỏng đối lập."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    await run_gate(
        sql="SELECT * FROM bronze.nguon",
        lakehouse_id=lakehouse_id,
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )


async def test_insert_into_select_requires_contributor(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="INSERT INTO bronze.dest SELECT * FROM bronze.src",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_self_referencing_insert_requires_contributor_and_is_still_a_read(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Chứng minh đỏ 6 (bắt buộc): `INSERT INTO t SELECT * FROM t` phải đòi
    `contributor` VÀ `t` vẫn phải nằm trong tập ĐỌC (`is_read=True`) — runner
    vẫn phải quét nó (xem `test_query_ctas.py::
    test_self_referencing_insert_still_scans_the_table` cho vế runner thật)."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="INSERT INTO ns.t SELECT * FROM ns.t",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )

    fake_authz.grant(lakehouse_id, "contributor")
    refs = await run_gate(
        sql="INSERT INTO ns.t SELECT * FROM ns.t",
        lakehouse_id=lakehouse_id,
        workspace_id=uuid.uuid4(),
        principal=principal,
        authz=fake_authz,
        resolver=fake_authz,
    )
    assert len(refs) == 1
    assert refs[0].is_read is True
    assert refs[0].is_write is True


async def test_write_in_a_cross_lakehouse_query_requires_contributor_on_that_lakehouse(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """Quyền GHI được hỏi ĐÚNG lakehouse sở hữu đích, không phải lakehouse của
    request: đích (`other.finance.reports`) thuộc `other_id`, và `other_id`
    được cấp `viewer` — KHÔNG đủ để ghi — trong khi `lakehouse_id` của chính
    request (chỉ đọc `aaa.orders`) được cấp `contributor` (thừa, không cần).
    Nếu `run_gate` nhầm lẫn áp yêu cầu ghi lên SAI lakehouse, bài này sẽ xanh
    một cách tình cờ dù lỗ hổng vẫn còn."""
    workspace_id = uuid.uuid4()
    lakehouse_id = uuid.uuid4()
    other_id = uuid.uuid4()
    fake_authz.register_lakehouse(workspace_id, "other", other_id)
    fake_authz.grant(lakehouse_id, "contributor")
    fake_authz.grant(other_id, "viewer")

    with pytest.raises(QueryForbidden):
        await run_gate(
            sql="CREATE TABLE other.finance.reports AS SELECT * FROM aaa.orders",
            lakehouse_id=lakehouse_id,
            workspace_id=workspace_id,
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )


async def test_read_json_is_still_rejected_only_two_readers_were_widened(
    fake_authz: FakeAuthz, principal: Principal
) -> None:
    """`read_json` là một hàm đọc file mà `FILE_READ_FUNCTIONS` KHÔNG nhận
    KHÔNG được Task 13 phục vụ — vẫn `ExternalSourceRejected` NGUYÊN VẸN, kể
    cả với một path nằm trong `Files/` (path an toàn không cứu được một hàm
    không được phục vụ).

    Quyền viewer CẤP SẴN — cùng lý do đã ghi ở bài `generate_series` phía
    trên: gỡ cổng nhãn làm `read_json('Files/a.json')` lọt qua HOÀN TOÀN
    (path AN TOÀN, `validate_files_paths` không có gì để từ chối), không bị
    che bởi một 403 không liên quan."""
    lakehouse_id = uuid.uuid4()
    fake_authz.grant(lakehouse_id, "viewer")
    with pytest.raises(ExternalSourceRejected):
        await run_gate(
            sql="SELECT * FROM read_json('Files/a.json')",
            lakehouse_id=lakehouse_id,
            workspace_id=uuid.uuid4(),
            principal=principal,
            authz=fake_authz,
            resolver=fake_authz,
        )
