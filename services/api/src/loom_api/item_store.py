"""CRUD item, version và ETag.

Thứ tự trong `create` quan trọng: validate definition TRƯỚC khi kiểm quyền hay
chạm database. Một definition sai là lỗi 422 của client, và không có lý do gì để
nó tốn một round trip."""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.models import ACTIVE, DEFAULT_TENANT_ID, Item, ItemVersion, Workspace
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
