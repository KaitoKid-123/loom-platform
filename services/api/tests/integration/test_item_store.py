import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.item_store import ItemStore, NameTaken, VersionMismatch
from loom_api.models import DELETED, Item, ItemVersion
from loom_api.pagination import CursorMismatch
from loom_api.permissions import Forbidden, NotVisible
from loom_core.item_definitions import ItemType, canonical_hash
from loom_core.roles import Role
from loom_core.schemas import Principal

pytestmark = pytest.mark.integration


async def _count_versions(session: AsyncSession, item_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(ItemVersion).where(ItemVersion.item_id == item_id)
        )
    ).scalar_one()


async def _row(session: AsyncSession, item_id: uuid.UUID) -> Any:
    """Trạng thái hàng `item` như POSTGRES thấy, không như identity map thấy.

    `select(Item)` trả lại chính đối tượng đang nằm trong session, nên khẳng
    định trên nó chỉ đọc lại thứ vừa gán trong Python — nó mù với một `flush()`
    thiếu, với một cột không được ghi, và với một UPDATE bị transaction khác đè.
    Chọn theo CỘT thì kết quả tới từ database.
    """
    return (
        await session.execute(
            select(
                Item.version,
                Item.definition,
                Item.definition_hash,
                Item.display_name,
                Item.folder_path,
                Item.description,
                Item.state,
                Item.updated_at,
                Item.updated_by,
            ).where(Item.id == item_id)
        )
    ).one()


async def test_contributor_can_create(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="bao-cao",
        display_name="Báo cáo",
        definition={"schema_version": 1, "sql": "SELECT 1"},
    )
    assert item.version == 1
    # Cái được lưu là definition đã CHUẨN HOÁ qua pydantic, không phải dict thô:
    # `visualization` không có trong đầu vào vẫn xuất hiện với giá trị None. Ghim
    # lại ở đây vì `definition_hash` là dấu vết dùng cho Git drift ở Giai đoạn 5
    # — hình dạng đã lưu đổi thì mọi item trông như vừa bị sửa.
    stored = {"schema_version": 1, "sql": "SELECT 1", "visualization": None}
    assert item.definition == stored
    # `assert item.definition_hash` đúng với BẤT KỲ chuỗi khác rỗng nào, kể cả
    # một hằng số — nó không nhìn thấy được thứ nó đặt tên.
    assert item.definition_hash == canonical_hash(stored)


async def test_viewer_cannot_create(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(Forbidden):
        await store.create(
            workspace_id=f.ws_a,
            item_type=ItemType.sql_script,
            name="x",
            display_name="X",
            definition={"schema_version": 1, "sql": ""},
        )


async def test_stranger_gets_404_not_403_when_creating(rbac_fixture):
    """Không có quyền gì trên workspace → 404. Trả 403 là xác nhận workspace tồn
    tại, mà tên workspace cũng mang thông tin."""
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(NotVisible):
        await store.create(
            workspace_id=f.ws_a,
            item_type=ItemType.sql_script,
            name="x",
            display_name="X",
            definition={"schema_version": 1, "sql": ""},
        )


async def test_duplicate_active_name_rejected_with_a_clear_error(rbac_fixture):
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    kwargs = dict(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="trung",
        display_name="Trùng",
        definition={"schema_version": 1, "sql": ""},
    )
    await store.create(**kwargs)
    with pytest.raises(NameTaken):
        await store.create(**kwargs)


async def test_an_unrelated_integrity_error_is_not_reported_as_a_name_clash(rbac_fixture):
    """Ánh xạ MỌI IntegrityError thành 409 "trùng tên" là một thông báo sai gửi
    người vận hành đi sai hướng. Ở đây `created_by` trỏ vào một user không tồn
    tại, nên cái vỡ là FK chứ không phải index tên — và cái tên thì chưa ai dùng.

    Cấp quyền theo NHÓM là cách duy nhất dựng được tình huống này: cấp theo user
    sẽ vỡ FK ngay ở chính hàng cấp quyền, trước khi tới được `create`.
    """
    f = rbac_fixture
    await f.grant(group="nhom-ma", scope=("workspace", f.ws_a), role=Role.contributor)
    ghost = Principal(
        user_id=uuid.uuid4(),
        subject="ma",
        email="ma@loom.local",
        display_name="ma",
        groups=("nhom-ma",),
    )
    store = ItemStore(f.session, ghost, request_id="r1")
    with pytest.raises(IntegrityError):
        await store.create(
            workspace_id=f.ws_a,
            item_type=ItemType.sql_script,
            name="ten-chua-ai-dung",
            display_name="X",
            definition={"schema_version": 1, "sql": ""},
        )


async def test_name_can_be_reused_after_soft_delete(rbac_fixture):
    """Đây là lý do index unique phải có WHERE state = 'active'. Không có nó,
    người dùng bị chặn bởi một hàng họ không còn thấy."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    kwargs = dict(
        workspace_id=f.ws_a,
        item_type=ItemType.sql_script,
        name="tai-dung",
        display_name="Tái dùng",
        definition={"schema_version": 1, "sql": ""},
    )
    first = await store.create(**kwargs)
    await store.soft_delete(first.id)
    again = await store.create(**kwargs)
    assert again.id != first.id


async def test_invalid_definition_is_rejected_before_touching_the_database(rbac_fixture):
    """`pytest.raises(ValidationError)` MỘT MÌNH mù với nửa sau của tên test:
    ValidationError vẫn được ném y hệt nếu validate chạy SAU phép kiểm quyền và
    sau một round trip. `sql_log` ghi mọi câu lệnh thật sự gửi tới Postgres, nên
    nó mới là thứ nhìn thấy được 'trước khi chạm database'."""
    from pydantic import ValidationError

    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.contributor)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    mark = len(f.sql_log)
    with pytest.raises(ValidationError):
        await store.create(
            workspace_id=f.ws_a,
            item_type=ItemType.connection,
            name="c",
            display_name="C",
            definition={
                "schema_version": 1,
                "kind": "postgres",
                "host": "h",
                "port": 5432,
                "secret_ref": "mat-khau-that",
            },
        )
    assert f.statements_since(mark) == []


async def test_list_filters_by_permission_and_pages_in_the_database(rbac_fixture):
    """60 item, chỉ 3 thấy được. Trang đầu phải trả đúng 3 — nếu ai đó lấy một
    trang rồi lọc trong Python thì test này đỏ.

    Ba item thấy được là ba item CŨ NHẤT, và `updated_at` được đặt tường minh.
    Không có hai điều đó thì phép kiểm này chỉ đỏ theo xác suất: với id ngẫu
    nhiên và 62 hàng cùng `updated_at`, ba hàng thấy được nằm trọn trong 51 hàng
    đầu khoảng 55% số lần, nên bản lọc-trong-Python sẽ XANH quá nửa số lần chạy.
    Một phép kiểm chỉ bắt được lỗi một nửa số lần thì không phải phép kiểm.
    """
    from loom_api.models import DEFAULT_TENANT_ID, Item

    f = rbac_fixture
    # Xa trong quá khứ để chắc chắn nằm sau item của fixture (updated_at của
    # chúng là now()) trong thứ tự giảm dần.
    base = datetime(2020, 1, 1, tzinfo=UTC)
    expected: list[uuid.UUID] = []
    for i in range(60):
        iid = uuid.uuid4()
        f.session.add(
            Item(
                id=iid,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=f.ws_a,
                type="sql_script",
                name=f"nhieu-{i}",
                display_name=f"N{i}",
                definition={"schema_version": 1, "sql": ""},
                definition_hash="0" * 64,
                created_by=f.user_alice,
                updated_by=f.user_alice,
                updated_at=base - timedelta(seconds=i),
            )
        )
        if i >= 57:
            await f.session.flush()
            await f.grant(user=f.user_bob, scope=("item", iid), role=Role.viewer)
            expected.append(iid)
    await f.session.flush()

    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    page = await store.list_items(workspace_id=f.ws_a, limit=50)
    assert len(page.items) == 3
    # Khẳng định trên ĐÚNG BA id, không chỉ trên con số: "ba hàng" cũng đúng nếu
    # truy vấn trả về ba hàng SAI.
    assert [item.id for item in page.items] == expected
    assert page.next_cursor is None


async def test_paging_never_skips_or_repeats_when_updated_at_ties(rbac_fixture):
    """Khoá sắp xếp phải DUY NHẤT — đây là chỗ duy nhất quan sát được điều đó.

    Test unit của `encode_cursor`/`decode_cursor` chỉ thấy cursor mã hoá được cả
    `id`; nó hoàn toàn mù với việc mệnh đề keyset trong SQL có dùng `id` hay
    không. Mười bốn item ở đây dùng CHUNG một `updated_at` vì `now()` của
    Postgres là thời điểm bắt đầu transaction — đúng tình huống của một lô import
    — nên nếu cursor chỉ so `updated_at` thì trang sau hoặc mất hàng hoặc lặp
    hàng, và không có gì báo lỗi.
    """
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    for i in range(12):
        await f.add_item(f.ws_a, f"lo-nhap-{i}")

    stamps = (
        (
            await f.session.execute(
                select(Item.updated_at).where(Item.workspace_id == f.ws_a).distinct()
            )
        )
        .scalars()
        .all()
    )
    assert len(stamps) == 1, f"tiền đề của test hỏng — updated_at không đồng nhất: {stamps}"

    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(10):  # trần an toàn, 14 hàng / 5 mỗi trang = 3 trang
        page = await store.list_items(workspace_id=f.ws_a, limit=5, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert cursor is None, "không lật hết được trong 10 trang"

    # 12 item mới + item-a1 + item-a2 của fixture.
    assert len(seen) == 14, f"lật trang làm mất hàng: được {len(seen)} / 14"
    assert len(set(seen)) == 14, "lật trang lặp bản ghi"


async def test_stranger_listing_a_workspace_gets_404_not_an_empty_page(rbac_fixture):
    """Không thấy workspace → 404, chứ không phải một trang rỗng. Trang rỗng là
    câu trả lời cho 'workspace này không có gì', và hai câu đó khác nhau."""
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(NotVisible):
        await store.list_items(workspace_id=f.ws_a, limit=50)


async def test_a_cursor_from_one_filter_is_rejected_by_another(rbac_fixture):
    """`test_cursor_from_a_different_filter_is_rejected` chỉ chứng minh
    `decode_cursor` BIẾT từ chối. Nó mù hoàn toàn với việc `list_items` có thật
    sự truyền bộ lọc của chính nó vào cursor hay không: truyền `{}` thì test đơn
    vị kia vẫn xanh, mà cursor của trang `type=sql_script` vẫn dùng được cho truy
    vấn `type=pipeline` và trả về rác."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    for i in range(4):
        await f.add_item(f.ws_a, f"loc-{i}")

    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    page = await store.list_items(workspace_id=f.ws_a, limit=2, item_type=ItemType.sql_script)
    assert page.next_cursor, "tiền đề hỏng — cần một trang có cursor để mang sang bộ lọc khác"

    with pytest.raises(CursorMismatch):
        await store.list_items(
            workspace_id=f.ws_a,
            limit=2,
            cursor=page.next_cursor,
            item_type=ItemType.pipeline,
        )


# ------------------------------------------------------------ Task 17: update


async def test_update_bumps_version_and_writes_a_version_row(rbac_fixture, an_item):
    f, item = rbac_fixture, an_item
    before = await _row(f.session, item.id)
    # Tiền đề của hai khẳng định `updated_by` bên dưới: người tạo KHÔNG phải bob.
    assert before.updated_by == f.user_alice

    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    updated = await store.update(
        item.id,
        expected_version=1,
        definition={"schema_version": 1, "sql": "SELECT 2"},
    )
    assert updated.version == 2

    stored = {"schema_version": 1, "sql": "SELECT 2", "visualization": None}
    row = await _row(f.session, item.id)
    assert row.version == 2
    assert row.definition == stored
    # Hash phải đi theo nội dung. Không khẳng định điều này thì một bản cài đặt
    # quên cập nhật `definition_hash` vẫn xanh, và Git drift ở Giai đoạn 5 sẽ
    # báo "không có gì đổi" cho một item vừa bị viết lại.
    assert row.definition_hash == canonical_hash(stored)
    assert row.updated_by == f.user_bob
    # `updated_at` phải TIẾN. Không có vế này thì bỏ hẳn dòng gán vẫn xanh —
    # `updated_at` không có `onupdate` nên nó sẽ đứng nguyên ở thời điểm tạo, và
    # một item vừa bị viết lại vẫn nằm im ở cuối danh sách sắp theo `updated_at`.
    assert row.updated_at > before.updated_at

    versions = (
        (
            await f.session.execute(
                select(ItemVersion)
                .where(ItemVersion.item_id == item.id)
                .order_by(ItemVersion.version)
            )
        )
        .scalars()
        .all()
    )
    assert [v.version for v in versions] == [1, 2]
    # Hàng version phải mang nội dung MỚI. Ghi một hàng version rỗng, hay ghi
    # lại nội dung cũ, cũng cho ra "hai hàng" — phép đếm một mình không nhìn
    # thấy thứ nó đặt tên.
    assert versions[1].definition == stored
    # alice tạo, bob sửa: hai hàng version phải ghi hai người khác nhau. Cùng
    # một người ở cả hai thì khẳng định này đúng cả khi `created_by` bị bỏ qua.
    assert (versions[0].created_by, versions[1].created_by) == (f.user_alice, f.user_bob)


async def test_stale_version_is_412(rbac_fixture, an_item):
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    await store.update(item.id, expected_version=1, definition={"schema_version": 1, "sql": "A"})
    with pytest.raises(VersionMismatch) as exc:
        await store.update(
            item.id, expected_version=1, definition={"schema_version": 1, "sql": "B"}
        )
    assert exc.value.status_code == 412

    # Lần bị từ chối không được để lại dấu vết. Chỉ khẳng định là có ném ngoại lệ
    # thì vẫn xanh với một bản cài đặt ghi xong rồi mới phát hiện lệch version.
    row = await _row(f.session, item.id)
    assert row.version == 2
    assert row.definition["sql"] == "A"
    assert await _count_versions(f.session, item.id) == 2


async def test_update_that_changes_nothing_is_a_noop(rbac_fixture, an_item):
    """Không có quy tắc này thì lịch sử version đầy bản ghi trùng và rollback mất
    tác dụng — người dùng phải lần qua hai mươi version giống nhau."""
    f, item = rbac_fixture, an_item
    before = await _row(f.session, item.id)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    result = await store.update(
        item.id,
        expected_version=1,
        definition=dict(item.definition),
        display_name=before.display_name,
        folder_path=before.folder_path,
        description=before.description,
    )
    assert result.version == 1
    assert await _count_versions(f.session, item.id) == 1

    after = await _row(f.session, item.id)
    assert after.version == 1
    # `updated_at` cũng phải đứng yên: nó là khoá sắp xếp của danh sách, nên một
    # no-op có chạm vào nó sẽ đẩy item lên đầu mọi trang mà không ai đổi gì.
    assert after.updated_at == before.updated_at


async def test_rename_alone_is_detected_as_a_change(rbac_fixture, an_item):
    """ETag là `version`, không phải definition_hash — chính vì thế đổi tên MỘT
    MÌNH cũng phải sinh version mới. Xem spec mục 2.2."""
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    updated = await store.update(
        item.id,
        expected_version=1,
        definition=dict(item.definition),
        display_name="Tên mới",
    )
    assert updated.version == 2
    assert updated.display_name == "Tên mới"
    row = await _row(f.session, item.id)
    assert (row.version, row.display_name) == (2, "Tên mới")
    assert await _count_versions(f.session, item.id) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("folder_path", "/bao-cao"), ("description", "mô tả mới")],
)
async def test_a_folder_move_or_a_description_edit_is_a_change_too(
    rbac_fixture, an_item, field, value
):
    """`test_rename_alone_...` chỉ nhìn thấy vế `display_name` của quy tắc no-op.

    Bỏ vế `folder_path` hoặc vế `description` khỏi phép so thì test đó vẫn XANH,
    còn việc di chuyển thư mục hoặc sửa mô tả thì lặng lẽ biến mất — không hàng
    version nào ghi lại và không có gì để rollback về. Mỗi vế của quy tắc cần
    một phép kiểm nhìn thấy được đúng vế đó.

    Cũng là chỗ duy nhất gọi `update` mà KHÔNG truyền `definition`: nhánh giữ lại
    definition cũ không có test nào khác đi qua.
    """
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    updated = await store.update(item.id, expected_version=1, **{field: value})
    assert updated.version == 2

    row = await _row(f.session, item.id)
    assert getattr(row, field) == value
    assert row.version == 2
    # definition không được truyền thì phải giữ nguyên, không thành NULL/rỗng.
    assert row.definition == item.definition
    assert await _count_versions(f.session, item.id) == 2


async def test_viewer_cannot_update(rbac_fixture):
    """`require_item(..., item_update)` với `item_read` cho ra cùng kết quả ở
    MỌI test khác trong file này, vì mọi test khác đều chạy dưới contributor.
    Đây là phép kiểm phân biệt được hai hằng số đó."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(Forbidden):
        await store.update(f.item_a1, expected_version=1, display_name="X")


async def test_stranger_updating_gets_404_not_403(rbac_fixture):
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(NotVisible):
        await store.update(f.item_a1, expected_version=1, display_name="X")


async def test_an_invalid_definition_is_rejected_before_the_version_is_bumped(
    rbac_fixture, an_item
):
    """422 của client không được để lại nửa cái ghi."""
    from pydantic import ValidationError

    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(ValidationError):
        await store.update(
            item.id, expected_version=1, definition={"schema_version": 1, "khong-co-truong-nay": 1}
        )
    row = await _row(f.session, item.id)
    assert row.version == 1
    assert await _count_versions(f.session, item.id) == 1


# ------------------------------------------- Task 18: xoá mềm và restore


async def _versions(session: AsyncSession, item_id: uuid.UUID) -> list[ItemVersion]:
    return list(
        (
            await session.execute(
                select(ItemVersion)
                .where(ItemVersion.item_id == item_id)
                .order_by(ItemVersion.version)
            )
        )
        .scalars()
        .all()
    )


async def test_soft_delete_hides_but_keeps_versions(rbac_fixture, an_item):
    f, item = rbac_fixture, an_item
    definition_before = dict(item.definition)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    await store.soft_delete(item.id)

    with pytest.raises(NotVisible):
        await store.get(item.id)

    # Lịch sử version là thứ DUY NHẤT phục hồi được nội dung sau khi xoá; một
    # xoá mềm làm mất lịch sử chỉ là một lần xoá cứng chậm hơn.
    assert await _count_versions(f.session, item.id) == 1

    row = await _row(f.session, item.id)
    assert row.state == DELETED
    # Hàng phải còn NGUYÊN nội dung. `state='deleted'` cộng với một definition bị
    # dọn rỗng vẫn thoả mọi khẳng định ở trên, và vẫn là mất dữ liệu.
    assert row.definition == definition_before
    assert row.updated_by == f.user_bob
    # `version` KHÔNG được nhúc nhích: nó đánh số các bản nội dung, và xoá không
    # tạo ra nội dung nào. Bump ở đây để lại một số không có hàng `item_version`
    # tương ứng, và `restore_version` sẽ không tìm thấy nó.
    assert row.version == 1


async def test_a_soft_deleted_item_disappears_from_listings(rbac_fixture, an_item):
    """`get` và `list_items` đi qua hai bộ lọc `state` KHÁC nhau — một cái trong
    `ItemStore`, một cái trong `visible_items_select`. Bỏ cái thứ hai thì item đã
    xoá vẫn hiện trong Explorer dù mở ra là 404."""
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    before = await store.list_items(f.ws_a)
    assert item.id in {i.id for i in before.items}

    await store.soft_delete(item.id)
    after = await store.list_items(f.ws_a)
    assert item.id not in {i.id for i in after.items}


async def test_deleting_twice_is_a_404(rbac_fixture, an_item):
    """Lần xoá thứ hai không được âm thầm thành công: nó sẽ dời `updated_at` của
    một hàng mà người gọi tin là đã biến mất."""
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    await store.soft_delete(item.id)
    with pytest.raises(NotVisible):
        await store.soft_delete(item.id)


async def test_viewer_cannot_soft_delete(rbac_fixture):
    """Phép kiểm phân biệt `item_delete` với `item_read`; contributor có cả hai
    nên mọi test khác ở đây mù với việc đổi hằng số."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(Forbidden):
        await store.soft_delete(f.item_a1)


async def test_stranger_deleting_gets_404_not_403(rbac_fixture):
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(NotVisible):
        await store.soft_delete(f.item_a1)


async def test_restore_creates_a_new_version_instead_of_rewinding(rbac_fixture, an_item):
    """Lịch sử bất biến: restore version 1 khi đang ở version 3 sinh ra version 4
    mang nội dung của version 1. Nhờ vậy hoàn tác được cả cú hoàn tác."""
    f, item = rbac_fixture, an_item
    original = dict(item.definition)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    await store.update(item.id, expected_version=1, definition={"schema_version": 1, "sql": "V2"})
    await store.update(item.id, expected_version=2, definition={"schema_version": 1, "sql": "V3"})

    restored = await store.restore_version(item.id, version=1)
    assert restored.version == 4
    assert restored.definition == original

    # `definition` đúng MỘT MÌNH không phân biệt được "sinh version mới" với
    # "lùi con trỏ về 1": cả hai đều để lại nội dung của version 1 trên hàng
    # item. Thứ phân biệt được là lịch sử vẫn còn đủ BỐN mốc, và version 3 vẫn
    # đọc lại được — tức cú hoàn tác này còn hoàn tác ngược lại được.
    rows = await _versions(f.session, item.id)
    assert [v.version for v in rows] == [1, 2, 3, 4]
    assert [v.definition["sql"] for v in rows] == [original["sql"], "V2", "V3", original["sql"]]

    row = await _row(f.session, item.id)
    assert row.version == 4
    assert row.definition_hash == canonical_hash(original)
    assert row.updated_by == f.user_bob
    # change_note phải nói version nào đã được phục hồi. Không có nó thì lịch sử
    # có hai mốc nội dung giống hệt nhau và không cách nào biết cái nào là bản
    # gốc, cái nào là bản khôi phục.
    assert rows[3].change_note is not None
    assert "1" in rows[3].change_note


async def test_restore_also_restores_metadata(rbac_fixture, an_item):
    """`item_version` lưu cả display_name/folder_path/description chính là để
    việc này được. Khôi phục mỗi definition thì một lần đổi tên là không hoàn
    tác được."""
    f, item = rbac_fixture, an_item
    original_name, original_folder = item.display_name, item.folder_path
    assert item.description is None
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    await store.update(
        item.id,
        expected_version=1,
        display_name="Đã đổi",
        folder_path="/noi-khac",
        description="mô tả thêm vào sau",
    )

    restored = await store.restore_version(item.id, version=1)
    row = await _row(f.session, item.id)
    assert (restored.display_name, restored.folder_path) == (original_name, original_folder)
    assert (row.display_name, row.folder_path) == (original_name, original_folder)
    # Phải quay về NULL. Một bản cài đặt copy theo kiểu `x if x is not None else
    # giữ nguyên` khôi phục được hai trường trên nhưng KHÔNG xoá lại được mô tả,
    # và hai khẳng định đầu vẫn xanh.
    assert restored.description is None
    assert row.description is None


async def test_restore_of_unknown_version_is_404(rbac_fixture, an_item):
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(NotVisible):
        await store.restore_version(item.id, version=99)
    # Và không để lại nửa cái ghi nào.
    assert (await _row(f.session, item.id)).version == 1
    assert await _count_versions(f.session, item.id) == 1


async def test_restore_cannot_reach_another_items_version(rbac_fixture, an_item):
    """Truy vấn nguồn phải lọc theo CẢ `item_id` lẫn `version`.

    Số version là cục bộ theo item, nên `WHERE version = 2` một mình khớp với
    hàng của mọi item trong database. Mọi test restore khác chỉ có đúng một item
    trong tầm nhìn, nên chúng mù với việc bỏ vế `item_id` — và thứ bị bỏ sót ở
    đây là nội dung của một item trong workspace mà người gọi không hề được xem.
    """
    f, item = rbac_fixture, an_item
    await f.grant(user=f.user_alice, scope=("workspace", f.ws_b), role=Role.contributor)
    other_store = ItemStore(f.session, f.principal_alice, request_id="fixture")
    other = await other_store.create(
        workspace_id=f.ws_b,
        item_type=ItemType.sql_script,
        name="cua-nguoi-khac",
        display_name="Của người khác",
        definition={"schema_version": 1, "sql": "BI MAT"},
    )
    await other_store.update(
        other.id, expected_version=1, definition={"schema_version": 1, "sql": "BI MAT 2"}
    )
    # Tiền đề: item kia CÓ version 2 còn item của bob thì không.
    assert await _count_versions(f.session, other.id) == 2
    assert await _count_versions(f.session, item.id) == 1

    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(NotVisible):
        await store.restore_version(item.id, version=2)
    row = await _row(f.session, item.id)
    assert row.version == 1
    assert row.definition["sql"] != "BI MAT 2"


async def test_restoring_a_deleted_item_is_a_404(rbac_fixture, an_item):
    """Restore đi qua cùng một `_lock_active` với update, nên nó chỉ chạy trên
    item đang sống. Không có thao tác bỏ-xoá trong Giai đoạn 1b; khôi phục một
    item đã xoá là một thao tác KHÁC và chưa tồn tại."""
    f, item = rbac_fixture, an_item
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    await store.soft_delete(item.id)
    with pytest.raises(NotVisible):
        await store.restore_version(item.id, version=1)


async def test_viewer_cannot_restore(rbac_fixture):
    """Không dùng `an_item`: fixture đó gán bob contributor, và một người mang
    cả hai vai trò vẫn được tính là contributor — phép kiểm sẽ không kiểm gì."""
    f = rbac_fixture
    await f.grant(user=f.user_bob, scope=("workspace", f.ws_a), role=Role.viewer)
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    with pytest.raises(Forbidden):
        await store.restore_version(f.item_a1, version=1)
