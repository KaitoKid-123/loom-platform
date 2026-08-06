"""Đọc audit — `member` trở lên.

Cổng quyền nằm trong `AuditReader.list_for_workspace`, không lặp lại ở đây: viết
hai lần là mời gọi drift, và drift ở đây nghĩa là một `contributor` đọc được ai đã
sửa gì trong workspace.

`request_id` có trong mỗi dòng, và đó là điểm chính của endpoint này: nó là sợi dây
duy nhất nối một thay đổi trong database với dòng log của đúng request đã gây ra nó.
Không trả nó ra thì người vận hành có bảng audit và có log, mà không có cách nào
ghép hai thứ lại.
"""

import uuid

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.audit import AuditReader
from loom_api.deps import PrincipalDep, SessionDep
from loom_api.models import AuditLog
from loom_core.schemas import PageOut, Principal

router = APIRouter(tags=["audit"])

_MAX_LIMIT = 200


def _out(row: AuditLog) -> dict[str, object]:
    return {
        "id": str(row.id),
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id),
        "actor_user_id": str(row.actor_user_id),
        "request_id": row.request_id,
        "summary": row.summary,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/workspaces/{workspace_id}/audit", response_model=PageOut)
async def list_audit(
    workspace_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
    # Spec mục 6 viết endpoint này là `/audit?resource_id=&workspace_id=`. Ở đây
    # `workspace_id` nằm trong ĐƯỜNG DẪN có chủ đích: một endpoint audit phẳng là đúng
    # hình dạng đã làm `search` thành chỗ dễ rò rỉ nhất của API — không có gì trong
    # đường dẫn nhắc người viết phải lọc quyền. Với workspace trên path thì cổng quyền
    # là điều đầu tiên đập vào mắt người đọc handler.
    resource_id: uuid.UUID | None = None,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    reader = AuditReader(session, principal)
    page = await reader.list_for_workspace(
        workspace_id, limit=min(limit, _MAX_LIMIT), cursor=cursor, resource_id=resource_id
    )
    return PageOut(items=[_out(r) for r in page.items], next_cursor=page.next_cursor)
