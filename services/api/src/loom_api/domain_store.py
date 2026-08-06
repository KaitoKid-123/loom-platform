"""CRUD domain.

Domain theo đúng nghĩa của Fabric: một nhóm workspace theo lĩnh vực nghiệp vụ, và một
cấp scope để gán quyền MỘT LẦN cho cả nhóm workspace thay vì gán lại trên từng cái.

Bảng `domain`, cột `workspace.domain_id` và cấp scope `domain` trong chuỗi tổ tiên đều
đã tồn tại và đang chạy từ Giai đoạn 1a — thiếu duy nhất một đường để tạo ra domain.
Spec mục 6 liệt kê bề mặt API và bỏ sót nó, trong khi mục 10 lại đòi tạo được domain qua
UI; hai mục đó mâu thuẫn và file này xử theo mục 10.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.audit import AuditWriter
from loom_api.integrity import constraint_of
from loom_api.models import ACTIVE, DEFAULT_TENANT_ID, Domain, Workspace
from loom_api.permissions import Forbidden, NotVisible, PermissionService
from loom_core.roles import Action, Role
from loom_core.schemas import Principal

_DOMAIN_NAME_INDEX = "uq_domain_tenant_name"


class DomainNameTaken(HTTPException):
    def __init__(self, name: str) -> None:
        super().__init__(status.HTTP_409_CONFLICT, f"a domain named '{name}' already exists")


class DomainStore:
    def __init__(self, session: AsyncSession, principal: Principal, request_id: str) -> None:
        self._session = session
        self._principal = principal
        self._perms = PermissionService(session, principal)
        self._audit = AuditWriter(session, principal, request_id)

    @property
    def perms(self) -> PermissionService:
        return self._perms

    async def list_domains(self) -> list[tuple[Domain, int, Role | None]]:
        """Mọi domain của tenant, kèm số workspace và vai trò của NGƯỜI GỌI trên từng cái.

        Không lọc theo quyền, và đó là chủ đích: danh sách domain là bản đồ tổ chức —
        biết phòng Tài chính tồn tại không phải là đọc được dữ liệu của họ. Đây là chỗ
        cố ý KHÁC với workspace, nơi không có quyền nghĩa là không thấy.

        Số workspace đếm bằng subquery trong CÙNG câu lệnh, không phải một vòng lặp gọi
        `count()` cho mỗi domain — mười domain sẽ thành mười một round trip.
        """
        counter = (
            select(Workspace.domain_id, func.count().label("n"))
            .where(Workspace.state == ACTIVE)
            .group_by(Workspace.domain_id)
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(Domain, func.coalesce(counter.c.n, 0))
                .outerjoin(counter, counter.c.domain_id == Domain.id)
                .where(Domain.tenant_id == DEFAULT_TENANT_ID)
                .order_by(Domain.display_name)
            )
        ).all()

        out: list[tuple[Domain, int, Role | None]] = []
        for domain, count in rows:
            out.append((domain, int(count), await self._perms.effective_role_for_domain(domain.id)))
        return out

    async def create(self, name: str, display_name: str, description: str | None = None) -> Domain:
        # Domain chưa tồn tại nên không có scope nào thấp hơn tenant để hỏi — cùng khuôn
        # với `WorkspaceStore.create`.
        role = await self._perms.effective_role_for_tenant(DEFAULT_TENANT_ID)
        if role is None:
            raise NotVisible
        if role < Role.admin:
            raise Forbidden("only a tenant admin can create a domain")

        domain = Domain(
            id=uuid.uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            name=name,
            display_name=display_name,
            description=description,
            created_by=self._principal.user_id,
            updated_by=self._principal.user_id,
        )
        self._session.add(domain)
        self._audit.record(
            action="domain.create",
            resource_type="domain",
            resource_id=domain.id,
            # `workspace_id` là NULL cho thao tác cấp domain — đúng như spec mục 3.1 ghi
            # trên cột đó.
            workspace_id=None,
            summary={"name": name},
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_of(exc) != _DOMAIN_NAME_INDEX:
                raise
            raise DomainNameTaken(name) from exc
        return domain

    async def get(self, domain_id: uuid.UUID) -> Domain:
        domain = (
            await self._session.execute(
                select(Domain).where(Domain.id == domain_id, Domain.tenant_id == DEFAULT_TENANT_ID)
            )
        ).scalar_one_or_none()
        if domain is None:
            raise NotVisible
        return domain

    async def update(
        self,
        domain_id: uuid.UUID,
        display_name: str | None = None,
        description: str | None = None,
    ) -> Domain:
        # `domain.manage` chỉ có ở admin (xem ACTION_MATRIX), và ở cấp domain hoặc trên.
        await self._perms.require_domain(domain_id, Action.domain_manage)
        domain = await self.get(domain_id)

        new_display = display_name if display_name is not None else domain.display_name
        new_desc = description if description is not None else domain.description
        if new_display == domain.display_name and new_desc == domain.description:
            return domain

        domain.display_name = new_display
        domain.description = new_desc
        domain.updated_by = self._principal.user_id
        domain.updated_at = datetime.now(UTC)
        self._audit.record(
            action="domain.update",
            resource_type="domain",
            resource_id=domain.id,
            workspace_id=None,
            summary={"name": domain.name},
        )
        await self._session.flush()
        return domain
