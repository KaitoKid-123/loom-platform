"""Gán, thu và đọc vai trò.

**Hai đường dẫn tường minh cho hai phạm vi, không một route `/{scope_type}/…`
gộp chung.** `domain` và `tenant` KHÔNG có route nào ở đây, nên chúng trả 404 vì
không tồn tại — chứ không vì một phép kiểm nhớ chặn chúng. Thuộc tính có được từ
cấu trúc thì không hỏng được khi ai đó sửa phép kiểm, và một route bắt tất cả
`/{scope_type}/{id}/roles` còn khớp cả những đường không ai định tạo ra.

Thu vai trò nhận principal qua QUERY, không qua body. RFC 9110 nói client không
nên gửi nội dung trong DELETE, và một số gateway lược nó đi — nếu điều đó xảy ra
với một lệnh thu thì server nhận một yêu cầu thiếu đúng phần nói THU CỦA AI.
Ở đây nó sẽ ra 422 (fail an toàn), nhưng đặt tham số vào query thì tình huống đó
không tồn tại, và `curl -X DELETE '…?user_id=…'` gọi được ngay không cần cờ nào.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.role_store import RoleStore, Scope
from loom_core.roles import GRANTABLE_BY, Role
from loom_core.schemas import (
    Principal,
    PrincipalRef,
    RoleAssignmentOut,
    RoleGrant,
    RoleListOut,
)

router = APIRouter(tags=["roles"])

RevokeQuery = Annotated[PrincipalRef, Query()]


async def _list_roles(scope: Scope, session: AsyncSession, principal: Principal) -> RoleListOut:
    store = RoleStore(session, principal)
    # Cổng `role.read` nằm trong `list_roles`; nó cũng nạp sẵn vai trò của người
    # gọi vào cache, nên câu hỏi ngay dưới đây không thêm round trip nào.
    rows = await store.list_roles(scope)

    scope_type, scope_id = scope
    my_role = (
        await store.perms.effective_role_for_workspace(scope_id)
        if scope_type == "workspace"
        else await store.perms.effective_role_for_item(scope_id)
    )
    return RoleListOut(
        items=[
            RoleAssignmentOut(
                principal_type=r.principal_type,
                user_id=r.principal_user_id,
                group=r.principal_group,
                role=r.role,
            )
            for r in rows
        ],
        # Sắp theo thứ bậc vai trò, không theo bảng chữ cái: giao diện hiện đúng
        # thứ tự này trong ô chọn, và "admin, contributor, member, viewer" đọc như
        # một danh sách ngẫu nhiên.
        grantable_roles=(
            sorted((str(r) for r in GRANTABLE_BY[my_role]), key=lambda name: Role[name])
            if my_role is not None
            else []
        ),
    )


async def _grant(
    scope: Scope, body: RoleGrant, session: AsyncSession, principal: Principal
) -> Response:
    store = RoleStore(session, principal)
    await store.grant(
        scope=scope,
        role=Role[body.role],
        user_id=body.user_id,
        group=body.group,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _revoke(
    scope: Scope, who: PrincipalRef, session: AsyncSession, principal: Principal
) -> Response:
    store = RoleStore(session, principal)
    await store.revoke(scope=scope, user_id=who.user_id, group=who.group)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/workspaces/{workspace_id}/roles", response_model=RoleListOut)
async def list_workspace_roles(
    workspace_id: uuid.UUID,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> RoleListOut:
    return await _list_roles(("workspace", workspace_id), session, principal)


@router.put("/workspaces/{workspace_id}/roles", status_code=status.HTTP_204_NO_CONTENT)
async def grant_workspace_role(
    workspace_id: uuid.UUID,
    body: RoleGrant,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _grant(("workspace", workspace_id), body, session, principal)


@router.delete("/workspaces/{workspace_id}/roles", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_workspace_role(
    workspace_id: uuid.UUID,
    who: RevokeQuery,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _revoke(("workspace", workspace_id), who, session, principal)


@router.get("/items/{item_id}/roles", response_model=RoleListOut)
async def list_item_roles(
    item_id: uuid.UUID,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> RoleListOut:
    return await _list_roles(("item", item_id), session, principal)


@router.put("/items/{item_id}/roles", status_code=status.HTTP_204_NO_CONTENT)
async def grant_item_role(
    item_id: uuid.UUID,
    body: RoleGrant,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _grant(("item", item_id), body, session, principal)


@router.delete("/items/{item_id}/roles", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_item_role(
    item_id: uuid.UUID,
    who: RevokeQuery,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _revoke(("item", item_id), who, session, principal)
