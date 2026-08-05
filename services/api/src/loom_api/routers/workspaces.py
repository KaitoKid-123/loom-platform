import uuid

from fastapi import APIRouter, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.models import Workspace
from loom_api.workspace_store import WorkspaceStore
from loom_core.schemas import PageOut, Principal, WorkspaceCreate, WorkspaceOut

router = APIRouter(tags=["workspaces"])

_MAX_LIMIT = 200


def _store(request: Request, session: AsyncSession, principal: Principal) -> WorkspaceStore:
    # request_id do RequestContextMiddleware của Giai đoạn 0 gắn vào state. Dùng
    # getattr vì middleware không chạy trong một số bài test dựng app trần.
    return WorkspaceStore(session, principal, getattr(request.state, "request_id", "-"))


async def _out(store: WorkspaceStore, ws: Workspace) -> WorkspaceOut:
    role = await store.perms.effective_role_for_workspace(ws.id)
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        display_name=ws.display_name,
        description=ws.description,
        domain_id=ws.domain_id,
        # Vai trò của NGƯỜI GỌI. `effective_role_for_workspace` hỏi theo principal
        # của store, nên đây không thể là vai trò của người khác.
        my_role=str(role) if role else "",
    )


@router.get("/workspaces", response_model=PageOut)
async def list_workspaces(
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    store = _store(request, session, principal)
    page = await store.list_workspaces(limit=min(limit, _MAX_LIMIT), cursor=cursor)
    return PageOut(
        items=[(await _out(store, w)).model_dump(mode="json") for w in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/workspaces", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    request: Request,
    body: WorkspaceCreate,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> WorkspaceOut:
    store = _store(request, session, principal)
    ws = await store.create(
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        domain_id=body.domain_id,
    )
    out = await _out(store, ws)
    # Commit ở tầng router, không ở store: dòng audit phải chia chung transaction
    # với thao tác, và store không được quyết định khi nào transaction kết thúc.
    await session.commit()
    return out


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    request: Request,
    workspace_id: uuid.UUID,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> WorkspaceOut:
    store = _store(request, session, principal)
    return await _out(store, await store.get(workspace_id))


@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    request: Request,
    workspace_id: uuid.UUID,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    store = _store(request, session, principal)
    await store.soft_delete(workspace_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
