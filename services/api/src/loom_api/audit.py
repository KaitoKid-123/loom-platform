"""Ghi và đọc audit log.

`AuditWriter.record()` là hàm ĐỒNG BỘ và chỉ gọi `session.add()`. Không commit,
không background task. Chính điều đó khiến dòng audit chia chung số phận với thao
tác nó ghi lại: thao tác rollback thì audit rollback theo, và không bao giờ có một
thay đổi không dấu vết — đúng thứ audit tồn tại để ngăn.

Nếu `record()` tự commit, hoặc chạy trong một session riêng, thì một process chết
giữa hai bước để lại một trong hai: một thay đổi không ai biết, hoặc một dòng
audit nói về thay đổi chưa từng xảy ra. Cả hai đều tệ hơn không có audit, vì
chúng làm người đọc tin vào một thứ sai.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.models import DEFAULT_TENANT_ID, AuditLog
from loom_api.pagination import Page, decode_cursor, encode_cursor
from loom_api.permissions import PermissionService
from loom_core.roles import Action
from loom_core.schemas import Principal


class AuditWriter:
    def __init__(self, session: AsyncSession, principal: Principal, request_id: str) -> None:
        self._session = session
        self._principal = principal
        self._request_id = request_id

    def record(
        self,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID,
        summary: dict[str, Any],
        workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Thêm một dòng audit vào session hiện tại. KHÔNG commit.

        Đồng bộ có chủ đích: không có `await` nào ở đây thì không có chỗ nào cho
        một `await` khác chen vào giữa thao tác và dấu vết của nó.
        """
        self._session.add(
            AuditLog(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                actor_user_id=self._principal.user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                workspace_id=workspace_id,
                request_id=self._request_id,
                summary=summary,
            )
        )


class AuditReader:
    """Đọc audit đòi `member` trở lên.

    `audit_read` chỉ xuất hiện trong `ACTION_MATRIX` từ `member` lên, nên quy tắc
    nằm ở `roles.py` chứ không nằm ở đây — file này chỉ hỏi. Một `contributor`
    sửa được item nhưng không cần biết ai khác đã sửa gì.
    """

    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self._session = session
        self._perms = PermissionService(session, principal)

    async def list_for_workspace(
        self,
        workspace_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
        resource_id: uuid.UUID | None = None,
    ) -> Page:
        await self._perms.require_workspace(workspace_id, Action.audit_read)

        # `resource_id` nằm TRONG dấu vết bộ lọc của cursor: đổi bộ lọc mà giữ cursor cũ
        # cho ra một trang lấy từ giữa một tập khác, và `decode_cursor` từ chối đúng vì
        # thế. Xem `pagination.py`.
        filters: dict[str, Any] = {"workspace_id": str(workspace_id)}
        stmt = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
        if resource_id is not None:
            # Lọc THÊM, không thay: `workspace_id` vẫn ở đó, nên một `resource_id` của
            # workspace khác cho ra rỗng chứ không cho đọc chéo.
            filters["resource_id"] = str(resource_id)
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if cursor:
            after_ts, after_id = decode_cursor(cursor, filters)
            # Cùng mẫu keyset như item: `created_at` một mình KHÔNG duy nhất — mọi
            # dòng audit của cùng một request chia nhau một `now()` vì đó là thời
            # điểm bắt đầu transaction.
            stmt = stmt.where(
                (AuditLog.created_at < after_ts)
                | ((AuditLog.created_at == after_ts) & (AuditLog.id < after_id))
            )
        stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return Page.build(
            rows,
            limit,
            cursor_of=lambda r: encode_cursor(r.created_at, r.id, filters),
        )
