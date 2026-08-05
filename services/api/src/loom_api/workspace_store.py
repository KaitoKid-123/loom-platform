"""CRUD workspace."""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.audit import AuditWriter
from loom_api.integrity import constraint_of
from loom_api.models import ACTIVE, DEFAULT_TENANT_ID, DELETED, Workspace
from loom_api.pagination import Page, decode_cursor, encode_cursor
from loom_api.permissions import (
    Forbidden,
    NotVisible,
    PermissionService,
    visible_workspaces_select,
)
from loom_core.roles import Action, Role
from loom_core.schemas import Principal

_WORKSPACE_NAME_INDEX = "uq_workspace_active_name"


class NameTaken(HTTPException):
    def __init__(self, name: str) -> None:
        super().__init__(status.HTTP_409_CONFLICT, f"đã có workspace tên '{name}'")


class WorkspaceStore:
    def __init__(self, session: AsyncSession, principal: Principal, request_id: str) -> None:
        self._session = session
        self._principal = principal
        self._perms = PermissionService(session, principal)
        self._audit = AuditWriter(session, principal, request_id)

    @property
    def perms(self) -> PermissionService:
        return self._perms

    async def list_workspaces(self, limit: int = 50, cursor: str | None = None) -> Page:
        filters: dict[str, object] = {"scope": "workspaces"}
        stmt = visible_workspaces_select(self._principal)
        if cursor:
            after_ts, after_id = decode_cursor(cursor, filters)
            stmt = stmt.where(
                (Workspace.updated_at < after_ts)
                | ((Workspace.updated_at == after_ts) & (Workspace.id < after_id))
            )
        stmt = stmt.order_by(Workspace.updated_at.desc(), Workspace.id.desc()).limit(limit + 1)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return Page.build(
            rows,
            limit,
            cursor_of=lambda w: encode_cursor(w.updated_at, w.id, filters),
        )

    async def get(self, workspace_id: uuid.UUID) -> Workspace:
        await self._perms.require_workspace(workspace_id, Action.workspace_read)
        ws = (
            await self._session.execute(
                select(Workspace).where(Workspace.id == workspace_id, Workspace.state == ACTIVE)
            )
        ).scalar_one_or_none()
        if ws is None:
            # require_workspace đã qua nghĩa là có assignment, nhưng workspace có
            # thể vừa bị xoá mềm. Với client thì nó không còn tồn tại.
            raise NotVisible
        return ws

    async def create(
        self,
        name: str,
        display_name: str,
        description: str | None = None,
        domain_id: uuid.UUID | None = None,
    ) -> Workspace:
        # Workspace chưa tồn tại nên không có scope nào thấp hơn tenant để hỏi.
        role = await self._perms.effective_role_for_tenant(DEFAULT_TENANT_ID)
        if role is None:
            raise NotVisible
        if role < Role.admin:
            raise Forbidden("chỉ admin cấp tenant tạo được workspace")

        ws_id = uuid.uuid4()
        ws = Workspace(
            id=ws_id,
            tenant_id=DEFAULT_TENANT_ID,
            domain_id=domain_id,
            name=name,
            display_name=display_name,
            description=description,
            # Theo ĐÚNG bố cục ở spec mục 5.1, và theo ID chứ không theo tên: đổi
            # tên workspace không được làm đổi vị trí dữ liệu trên object storage
            # ở Giai đoạn 2.
            storage_prefix=f"workspaces/{ws_id}",
            created_by=self._principal.user_id,
            updated_by=self._principal.user_id,
        )
        self._session.add(ws)
        self._audit.record(
            action="workspace.create",
            resource_type="workspace",
            resource_id=ws.id,
            workspace_id=ws.id,
            summary={"name": name},
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            # Chỉ ánh xạ ĐÚNG constraint tên. Một FK `domain_id` trỏ vào domain
            # không tồn tại cũng ra IntegrityError, và báo "đã có workspace tên X"
            # cho một cái tên chưa ai dùng gửi người vận hành đi sai hướng.
            if constraint_of(exc) != _WORKSPACE_NAME_INDEX:
                raise
            raise NameTaken(name) from exc
        return ws

    async def soft_delete(self, workspace_id: uuid.UUID) -> None:
        await self._perms.require_workspace(workspace_id, Action.workspace_delete)
        ws = (
            await self._session.execute(
                select(Workspace)
                .where(Workspace.id == workspace_id, Workspace.state == ACTIVE)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if ws is None:
            raise NotVisible
        ws.state = DELETED
        ws.updated_by = self._principal.user_id
        ws.updated_at = datetime.now(UTC)
        self._audit.record(
            action="workspace.delete",
            resource_type="workspace",
            resource_id=ws.id,
            workspace_id=ws.id,
            summary={"name": ws.name},
        )
        await self._session.flush()
