import uuid

from fastapi import APIRouter, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.etag import etag_for, parse_if_match
from loom_api.item_store import ItemStore
from loom_api.models import Item
from loom_core.item_definitions import ItemType
from loom_core.schemas import ItemCreate, ItemOut, ItemPatch, PageOut, Principal

router = APIRouter(tags=["items"])

_MAX_LIMIT = 200


def _out(item: Item) -> ItemOut:
    return ItemOut(
        id=item.id,
        workspace_id=item.workspace_id,
        type=item.type,
        name=item.name,
        display_name=item.display_name,
        folder_path=item.folder_path,
        description=item.description,
        definition=item.definition,
        version=item.version,
        updated_at=item.updated_at,
    )


def _store(request: Request, session: AsyncSession, principal: Principal) -> ItemStore:
    return ItemStore(session, principal, getattr(request.state, "request_id", "-"))


@router.get("/workspaces/{workspace_id}/items", response_model=PageOut)
async def list_items(
    request: Request,
    workspace_id: uuid.UUID,
    # Cùng lý do như `ItemCreate.type`: `ItemType(type)` ở dưới là một constructor
    # nhận thẳng dữ liệu client gửi, và một loại không tồn tại thành 500.
    type: ItemType | None = None,
    folder: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    store = _store(request, session, principal)
    page = await store.list_items(
        workspace_id=workspace_id,
        limit=min(limit, _MAX_LIMIT),
        cursor=cursor,
        item_type=type,
        folder=folder,
    )
    return PageOut(
        items=[_out(i).model_dump(mode="json") for i in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/workspaces/{workspace_id}/items",
    response_model=ItemOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_item(
    request: Request,
    workspace_id: uuid.UUID,
    body: ItemCreate,
    response: Response,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> ItemOut:
    store = _store(request, session, principal)
    item = await store.create(
        workspace_id=workspace_id,
        item_type=body.type,
        name=body.name,
        display_name=body.display_name,
        definition=body.definition,
        folder_path=body.folder_path,
        description=body.description,
    )
    out = _out(item)
    await session.commit()
    # ETag ngay trên phản hồi tạo: không có nó, client phải GET lại trước khi sửa
    # được thứ mình vừa tạo.
    response.headers["ETag"] = etag_for(out.version)
    return out


@router.get("/items/{item_id}", response_model=ItemOut)
async def get_item(
    request: Request,
    item_id: uuid.UUID,
    response: Response,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> ItemOut:
    store = _store(request, session, principal)
    item = await store.get(item_id)
    response.headers["ETag"] = etag_for(item.version)
    return _out(item)


@router.patch("/items/{item_id}", response_model=ItemOut)
async def patch_item(
    request: Request,
    item_id: uuid.UUID,
    body: ItemPatch,
    response: Response,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> ItemOut:
    expected = parse_if_match(request.headers.get("if-match"))
    store = _store(request, session, principal)
    item = await store.update(
        item_id,
        expected_version=expected,
        definition=body.definition,
        display_name=body.display_name,
        folder_path=body.folder_path,
        description=body.description,
        change_note=body.change_note,
    )
    out = _out(item)
    await session.commit()
    response.headers["ETag"] = etag_for(out.version)
    return out


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    request: Request,
    item_id: uuid.UUID,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> Response:
    store = _store(request, session, principal)
    await store.soft_delete(item_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/items/{item_id}/versions", response_model=PageOut)
async def list_versions(
    request: Request,
    item_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    store = _store(request, session, principal)
    page = await store.list_versions(item_id, limit=min(limit, _MAX_LIMIT), cursor=cursor)
    return PageOut(items=page.items, next_cursor=page.next_cursor)


@router.get("/items/{item_id}/versions/{version}")
async def get_version(
    request: Request,
    item_id: uuid.UUID,
    version: int,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> dict[str, object]:
    store = _store(request, session, principal)
    row = await store.get_version(item_id, version)
    return {
        "version": row.version,
        "display_name": row.display_name,
        "folder_path": row.folder_path,
        "description": row.description,
        "change_note": row.change_note,
        "created_at": row.created_at.isoformat(),
        "created_by": str(row.created_by),
        # `definition` CHỈ ở đây, không ở danh sách: danh sách hiện cho nhiều người hơn,
        # và với item `connection` thì definition mang `secret_ref`.
        "definition": row.definition,
    }


@router.post("/items/{item_id}/versions/{version}/restore", response_model=ItemOut)
async def restore_version(
    request: Request,
    item_id: uuid.UUID,
    version: int,
    response: Response,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> ItemOut:
    store = _store(request, session, principal)
    item = await store.restore_version(item_id, version=version)
    out = _out(item)
    await session.commit()
    response.headers["ETag"] = etag_for(out.version)
    return out
