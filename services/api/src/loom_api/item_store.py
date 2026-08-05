"""CRUD item, version và ETag.

Thứ tự trong `create` quan trọng: validate definition TRƯỚC khi kiểm quyền hay
chạm database. Một definition sai là lỗi 422 của client, và không có lý do gì để
nó tốn một round trip."""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.audit import AuditWriter
from loom_api.models import ACTIVE, DEFAULT_TENANT_ID, DELETED, Item, ItemVersion, Workspace
from loom_api.pagination import Page, decode_cursor, encode_cursor
from loom_api.permissions import (
    NotVisible,
    PermissionService,
    visible_items_select,
    visible_workspaces_select,
)
from loom_core.item_definitions import ItemType, canonical_hash, parse_definition
from loom_core.roles import Action
from loom_core.schemas import Principal

# Tên index unique MỘT PHẦN trên (workspace_id, type, name) WHERE state='active'.
# Viết cứng ở đây vì asyncpg chỉ trả về TÊN constraint, không trả về cột. Đổi tên
# index trong migration mà quên dòng này thì `create` trả 500 thay vì 409 —
# `test_duplicate_active_name_rejected_with_a_clear_error` bắt được ngay, nên chỗ
# này hỏng thành ồn ào chứ không hỏng thành im lặng.
_ITEM_NAME_INDEX = "uq_item_active_name"


class NameTaken(HTTPException):
    def __init__(self, name: str) -> None:
        super().__init__(
            status.HTTP_409_CONFLICT,
            f"đã có item tên '{name}' cùng loại trong workspace này",
        )


class VersionMismatch(HTTPException):
    """412, không phải 409: `If-Match` là một ĐIỀU KIỆN TIÊN QUYẾT của request.

    409 nói "yêu cầu xung đột với trạng thái hiện tại" mà không nói điều kiện nào
    đã bị kiểm, nên client không biết thử lại theo cách nào. 412 nói đúng một
    điều: cái bạn nói bạn đang sửa không còn là cái đang có.

    Mang theo `current` để tầng HTTP đặt được `ETag` của bản hiện tại vào phản
    hồi — bắt client gọi thêm một GET chỉ để biết mình lệch bao nhiêu là một
    round trip không cần thiết.
    """

    def __init__(self, current: int) -> None:
        super().__init__(
            status.HTTP_412_PRECONDITION_FAILED,
            f"item đã được người khác đổi (bản hiện tại {current}) — tải lại rồi thử lại",
        )
        self.current = current


def _constraint_of(exc: IntegrityError) -> str | None:
    """Tên constraint mà Postgres báo là đã vỡ, hoặc None nếu không moi ra được.

    SQLAlchemy bọc lỗi asyncpg hai lớp: `exc.orig` là shim DBAPI của dialect và
    KHÔNG có `constraint_name`; `exc.orig.__cause__` mới là exception asyncpg
    thật. Đi thẳng vào `exc.orig` là luôn nhận None, tức là mọi lỗi đều rơi vào
    nhánh "không phải trùng tên" — sai ngược lại và cũng âm thầm y hệt.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


class ItemStore:
    def __init__(self, session: AsyncSession, principal: Principal, request_id: str) -> None:
        self._session = session
        self._principal = principal
        self._request_id = request_id
        self._perms = PermissionService(session, principal)
        self._audit = AuditWriter(session, principal, request_id)

    async def create(
        self,
        workspace_id: uuid.UUID,
        item_type: ItemType,
        name: str,
        display_name: str,
        definition: dict[str, object],
        folder_path: str = "/",
        description: str | None = None,
    ) -> Item:
        # Validate TRƯỚC: lỗi 422 của client không nên tốn một round trip.
        parsed = parse_definition(item_type, definition)
        payload = parsed.model_dump(mode="json")

        await self._perms.require_workspace(workspace_id, Action.item_create)

        item = Item(
            id=uuid.uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            workspace_id=workspace_id,
            type=str(item_type),
            name=name,
            display_name=display_name,
            folder_path=folder_path,
            description=description,
            definition=payload,
            definition_hash=canonical_hash(payload),
            version=1,
            state=ACTIVE,
            created_by=self._principal.user_id,
            updated_by=self._principal.user_id,
        )
        self._session.add(item)
        self._session.add(
            ItemVersion(
                id=uuid.uuid4(),
                item_id=item.id,
                version=1,
                definition=payload,
                display_name=display_name,
                folder_path=folder_path,
                description=description,
                change_note="tạo mới",
                created_by=self._principal.user_id,
            )
        )
        # Ghi audit TRƯỚC `flush()`, trong cùng session: nếu lệnh chèn vỡ thì dòng
        # audit chưa bao giờ tới database. Ghi sau flush thành công cũng đúng, nhưng
        # đặt ở đây làm rõ rằng hai thứ đi cùng nhau chứ không phải nối tiếp.
        #
        # `summary` là TÓM TẮT, không nhúng `definition`: `item_version` đã giữ bản
        # đầy đủ, và với item `connection` thì `definition` mang `secret_ref`.
        self._audit.record(
            action="item.create",
            resource_type="item",
            resource_id=item.id,
            workspace_id=workspace_id,
            summary={"type": str(item_type), "name": name},
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            # Phải hỏi ĐÚNG constraint nào vỡ. Ánh xạ mọi IntegrityError thành
            # NameTaken là sai và đã kiểm được: một FK `created_by` trỏ vào user
            # không tồn tại cũng ra 409 "đã có item tên X" cho một cái tên chưa ai
            # dùng — thông báo đó gửi người vận hành đi sai hướng hoàn toàn.
            #
            # Index unique một phần chỉ phủ state='active', nên khi ĐÚNG là nó thì
            # lỗi này chỉ xảy ra với một item đang sống, không phải với một item đã
            # xoá mềm.
            if _constraint_of(exc) != _ITEM_NAME_INDEX:
                raise
            raise NameTaken(name) from exc
        return item

    async def get(self, item_id: uuid.UUID) -> Item:
        await self._perms.require_item(item_id, Action.item_read)
        item = (
            await self._session.execute(
                select(Item).where(Item.id == item_id, Item.state == ACTIVE)
            )
        ).scalar_one_or_none()
        if item is None:
            # require_item đã qua nghĩa là có assignment, nhưng item có thể vừa bị
            # xoá mềm. Với client thì nó không còn tồn tại.
            raise NotVisible
        return item

    async def _lock_active(self, item_id: uuid.UUID) -> Item:
        """Đọc item đang sống và KHOÁ hàng đó, trong một câu lệnh.

        `with_for_update` là phần khoá: hai PATCH đồng thời không được cùng đọc
        version 1, cùng thấy khớp, rồi cùng ghi version 2. Kiểm version mà không
        khoá hàng là một race, không phải một kiểm tra.

        `populate_existing` là phần khiến việc khoá có Ý NGHĨA. Mặc định của
        SQLAlchemy: truy vấn trả về hàng của một đối tượng đã nằm trong identity
        map thì trả lại chính đối tượng cũ và KHÔNG ghi đè thuộc tính đã nạp. Với
        một session đã đọc item từ trước — đường GET-rồi-PATCH bình thường —
        `FOR UPDATE` chờ đúng chỗ và nhận về hàng đã được bên kia bump, nhưng
        `item.version` trong bộ nhớ vẫn là số cũ và phép so vẫn khớp. Khoá hàng
        mới rồi so với bản chụp cũ thì cũng không phải một phép kiểm.

        Ba đường ghi dùng CHUNG hàm này thay vì chép lại câu select. Chép ba lần
        là ba cơ hội để một bản sao rụng mất một trong hai tuỳ chọn trên, và cả
        hai đều hỏng theo kiểu im lặng.
        """
        item = (
            await self._session.execute(
                select(Item)
                .where(Item.id == item_id, Item.state == ACTIVE)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if item is None:
            # require_* đã qua nghĩa là có assignment, nhưng hàng có thể vừa bị
            # xoá mềm. Với client thì nó không còn tồn tại.
            raise NotVisible
        return item

    async def update(
        self,
        item_id: uuid.UUID,
        expected_version: int,
        definition: dict[str, object] | None = None,
        display_name: str | None = None,
        folder_path: str | None = None,
        description: str | None = None,
        change_note: str | None = None,
    ) -> Item:
        await self._perms.require_item(item_id, Action.item_update)
        item = await self._lock_active(item_id)
        if item.version != expected_version:
            raise VersionMismatch(item.version)

        # Validate SAU khi đọc hàng, khác với `create`: loại item nằm trên hàng
        # đó, nên không có cách nào biết phải kiểm theo schema nào trước khi đọc.
        item_type = ItemType(item.type)
        new_definition = (
            parse_definition(item_type, definition).model_dump(mode="json")
            if definition is not None
            else dict(item.definition)
        )
        new_hash = canonical_hash(new_definition)
        new_display = display_name if display_name is not None else item.display_name
        new_folder = folder_path if folder_path is not None else item.folder_path
        new_desc = description if description is not None else item.description

        # No-op: so definition_hash CỘNG cả ba trường metadata. So hash một mình
        # là bỏ sót đổi tên — mà đổi tên là thay đổi thật và phải sinh version.
        # Đây chính là lý do ETag là `version` chứ không phải `definition_hash`.
        unchanged = (
            new_hash == item.definition_hash
            and new_display == item.display_name
            and new_folder == item.folder_path
            and new_desc == item.description
        )
        if unchanged:
            # Không bump, không ghi hàng version, không chạm `updated_at`. Bump
            # thì lịch sử đầy bản ghi trùng và rollback mất tác dụng; chạm
            # `updated_at` thì item nhảy lên đầu mọi danh sách mà không ai đổi gì.
            return item

        # Tính danh sách trường đã đổi TRƯỚC `_bump` — sau đó giá trị cũ không còn
        # để so nữa.
        changed = [
            field
            for field, old, new in (
                ("definition", item.definition_hash, new_hash),
                ("display_name", item.display_name, new_display),
                ("folder_path", item.folder_path, new_folder),
                ("description", item.description, new_desc),
            )
            if old != new
        ]
        self._bump(
            item,
            definition=new_definition,
            display_name=new_display,
            folder_path=new_folder,
            description=new_desc,
            change_note=change_note,
        )
        self._record_change(item, "item.update", {"changed": changed, "version": item.version})
        await self._session.flush()
        return item

    def _bump(
        self,
        item: Item,
        *,
        definition: dict[str, object],
        display_name: str,
        folder_path: str,
        description: str | None,
        change_note: str | None,
    ) -> None:
        """Nâng item lên một version mới và ghi hàng lịch sử tương ứng.

        `update` và `restore_version` dùng chung hàm này vì chúng là CÙNG một
        thao tác với hai nguồn nội dung khác nhau. Viết hai lần thì hash, dấu
        thời gian và người sửa có hai chỗ để trôi khỏi nhau, và bản sao bị bỏ
        quên sẽ sai theo kiểu không ai nhìn thấy.
        """
        item.definition = definition
        item.definition_hash = canonical_hash(definition)
        item.display_name = display_name
        item.folder_path = folder_path
        item.description = description
        item.version = item.version + 1
        item.updated_by = self._principal.user_id
        # Đặt tường minh, không dựa vào `now()` của Postgres: `now()` là thời
        # điểm bắt đầu TRANSACTION, nên hai lần sửa trong cùng một transaction sẽ
        # trùng dấu thời gian và khoá sắp xếp của phân trang mất tính duy nhất
        # theo đúng cách Task 16 đã gặp. Cột này cũng KHÔNG có `onupdate`, nên bỏ
        # dòng này đi thì `updated_at` đứng nguyên ở thời điểm tạo mãi mãi.
        item.updated_at = datetime.now(UTC)

        self._session.add(
            ItemVersion(
                id=uuid.uuid4(),
                item_id=item.id,
                version=item.version,
                definition=definition,
                display_name=display_name,
                folder_path=folder_path,
                description=description,
                change_note=change_note,
                created_by=self._principal.user_id,
            )
        )

    def _record_change(self, item: Item, action: str, summary: dict[str, object]) -> None:
        """Ghi audit cho một thay đổi trên item đã có.

        Gọi từ `update`/`restore_version`/`soft_delete` SAU khi đã biết chắc có thay
        đổi — một `PATCH` không đổi gì không được ghi gì, nếu không dấu vết đầy
        tiếng ồn và người đọc bắt đầu bỏ qua nó, tức mất tác dụng theo cách tệ nhất.
        """
        self._audit.record(
            action=action,
            resource_type="item",
            resource_id=item.id,
            workspace_id=item.workspace_id,
            summary=summary,
        )

    async def soft_delete(self, item_id: uuid.UUID) -> None:
        """Đánh dấu item là đã xoá. KHÔNG chạm `item_version`.

        Lịch sử version là thứ duy nhất phục hồi được nội dung sau một lần xoá,
        nên một xoá mềm làm mất lịch sử chỉ là một lần xoá cứng chậm hơn.

        Cũng KHÔNG bump `version`: version đánh số các bản NỘI DUNG, và xoá không
        tạo ra nội dung nào. Bump ở đây để lại một số không có hàng
        `item_version` nào tương ứng — `restore_version` sẽ không tìm thấy nó —
        và làm ETag của client đổi vì một lý do không phải là một lần sửa.

        Đi qua `_lock_active` nên xoá lần thứ hai là 404 chứ không phải một lần
        ghi im lặng dời `updated_at` của một hàng người gọi tưởng đã biến mất.
        """
        await self._perms.require_item(item_id, Action.item_delete)
        item = await self._lock_active(item_id)
        item.state = DELETED
        item.updated_by = self._principal.user_id
        item.updated_at = datetime.now(UTC)
        self._record_change(item, "item.delete", {"name": item.name})
        await self._session.flush()

    async def restore_version(self, item_id: uuid.UUID, version: int) -> Item:
        """Sinh version MỚI mang nội dung của một version cũ — không lùi con trỏ.

        Lịch sử bất biến nghĩa là hoàn tác được cả cú hoàn tác. Lùi con trỏ thì
        mọi bản ghi giữa hai mốc biến mất và không ai lấy lại được.

        Khôi phục cả metadata, không riêng definition: `item_version` lưu
        display_name/folder_path/description chính là để một lần đổi tên hay một
        lần chuyển thư mục cũng hoàn tác được.
        """
        await self._perms.require_item(item_id, Action.item_update)
        item = await self._lock_active(item_id)

        # Lọc theo CẢ hai cột. Số version là cục bộ theo item, nên `version = 2`
        # một mình khớp với hàng của mọi item trong database và restore sẽ kéo
        # nội dung của một item ở workspace khác vào đây.
        source = (
            await self._session.execute(
                select(ItemVersion).where(
                    ItemVersion.item_id == item_id, ItemVersion.version == version
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise NotVisible

        self._bump(
            item,
            definition=dict(source.definition),
            display_name=source.display_name,
            folder_path=source.folder_path,
            description=source.description,
            change_note=f"phục hồi từ version {version}",
        )
        self._record_change(
            item,
            "item.restore",
            {"from_version": version, "version": item.version},
        )
        await self._session.flush()
        return item

    async def _require_visible_workspace(self, workspace_id: uuid.UUID) -> None:
        """Cửa cho đường DANH SÁCH, và nó CỐ Ý không phải
        `require_workspace(workspace_read)`.

        `effective_role_for_workspace` chỉ chạy chuỗi tổ tiên TỪ workspace LÊN —
        `_chain_conditions(None, ...)` bỏ hẳn nhánh item — nên nó trả `None` cho
        người chỉ được chia sẻ lẻ một item bên trong workspace. Dùng nó ở đây thì
        đúng những người mà `visible_workspaces_select` cố ý cho THẤY workspace
        lại không MỞ được nó ra, và item được chia sẻ không còn đường nào tới.
        Đó chính là tình huống mà nhánh `by_item_inside` của Task 11 sinh ra để
        tránh.

        Nên hỏi lại đúng biểu thức của Task 11 thay vì viết bộ lọc thứ hai: hai
        định nghĩa "workspace này có thấy được không" là hai định nghĩa sẽ trôi
        khỏi nhau.
        """
        seen = (
            await self._session.execute(
                visible_workspaces_select(self._principal)
                .where(Workspace.id == workspace_id)
                .limit(1)
            )
        ).first()
        if seen is None:
            raise NotVisible

    async def list_items(
        self,
        workspace_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
        item_type: ItemType | None = None,
        folder: str | None = None,
    ) -> Page:
        await self._require_visible_workspace(workspace_id)

        filters = {
            "workspace_id": str(workspace_id),
            "type": str(item_type) if item_type else None,
            "folder": folder,
        }

        stmt = visible_items_select(self._principal, workspace_id=workspace_id)
        if item_type is not None:
            stmt = stmt.where(Item.type == str(item_type))
        if folder is not None:
            stmt = stmt.where(Item.folder_path == folder)

        if cursor:
            after_ts, after_id = decode_cursor(cursor, filters)
            # Điều kiện keyset: đúng cặp (updated_at, id) mới cho thứ tự tổng.
            stmt = stmt.where(
                (Item.updated_at < after_ts)
                | ((Item.updated_at == after_ts) & (Item.id < after_id))
            )

        # limit+1 để biết còn trang sau, không cần COUNT.
        stmt = stmt.order_by(Item.updated_at.desc(), Item.id.desc()).limit(limit + 1)
        rows = (await self._session.execute(stmt)).scalars().all()
        return Page.build(
            rows, limit, cursor_of=lambda it: encode_cursor(it.updated_at, it.id, filters)
        )
