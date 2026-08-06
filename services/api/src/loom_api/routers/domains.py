"""Endpoint domain.

Đọc thì AI CŨNG ĐƯỢC, tạo và sửa thì chỉ admin. Danh sách domain là bản đồ tổ chức —
biết phòng Tài chính tồn tại không phải là đọc được dữ liệu của họ — nên nó cố ý không
theo quy ước 404 của workspace, nơi không có quyền nghĩa là không thấy.
"""

import uuid

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.domain_store import DomainStore
from loom_api.models import Domain
from loom_core.roles import Role
from loom_core.schemas import DomainCreate, DomainOut, DomainPatch, PageOut, Principal

router = APIRouter(tags=["domains"])


def _store(request: Request, session: AsyncSession, principal: Principal) -> DomainStore:
    return DomainStore(session, principal, getattr(request.state, "request_id", "-"))


def _out(domain: Domain, count: int, role: Role | None) -> DomainOut:
    return DomainOut(
        id=domain.id,
        name=domain.name,
        display_name=domain.display_name,
        description=domain.description,
        workspace_count=count,
        my_role=str(role) if role else None,
    )


@router.get("/domains", response_model=PageOut)
async def list_domains(
    request: Request,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    store = _store(request, session, principal)
    rows = await store.list_domains()
    # `next_cursor` luôn None: một tenant có hàng chục domain, không hàng nghìn, và phân
    # trang một danh sách luôn vừa một màn hình chỉ thêm một khái niệm để hiểu sai. Giữ
    # `PageOut` để hình dạng phản hồi giống mọi danh sách khác.
    return PageOut(
        items=[_out(d, c, r).model_dump(mode="json") for d, c, r in rows], next_cursor=None
    )


@router.post("/domains", response_model=DomainOut, status_code=201)
async def create_domain(
    request: Request,
    body: DomainCreate,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> DomainOut:
    store = _store(request, session, principal)
    domain = await store.create(
        name=body.name, display_name=body.display_name, description=body.description
    )
    role = await store.perms.effective_role_for_domain(domain.id)
    await session.commit()
    # Domain vừa tạo chưa có workspace nào — không cần đếm lại.
    return _out(domain, 0, role)


@router.patch("/domains/{domain_id}", response_model=DomainOut)
async def patch_domain(
    request: Request,
    domain_id: uuid.UUID,
    body: DomainPatch,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> DomainOut:
    store = _store(request, session, principal)
    domain = await store.update(
        domain_id, display_name=body.display_name, description=body.description
    )
    role = await store.perms.effective_role_for_domain(domain.id)
    await session.commit()
    rows = await store.list_domains()
    count = next((c for d, c, _ in rows if d.id == domain.id), 0)
    return _out(domain, count, role)
