import re
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.item_store import ItemStore
from loom_api.models import Item
from loom_core.item_definitions import ItemType
from loom_core.schemas import ItemCreate, ItemOut, ItemPatch, PageOut, Principal

router = APIRouter(tags=["items"])

_MAX_LIMIT = 200

# Nhận cả `W/"7"` và `7`. Client HTTP và proxy viết lại ETag thường xuyên, và biến
# một chi tiết định dạng thành 412 là cách chắc chắn để người dùng tin rằng công
# việc của họ vừa bị mất.
_ETAG_RE = re.compile(r'^(?:W/)?"?(\d+)"?\Z')


def _parse_if_match(raw: str | None) -> int:
    if not raw:
        # 428 chứ không 400: nó nói cho client biết CHÍNH XÁC phải thêm header nào,
        # và một client tử tế sẽ tự thử lại đúng cách.
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "missing If-Match header — load the item to get its ETag, then send it back",
        )
    match = _ETAG_RE.match(raw.strip())
    if not match:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"malformed If-Match header: {raw}")
    return int(match.group(1))


def _etag(version: int) -> str:
    return f'W/"{version}"'


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
    response.headers["ETag"] = _etag(out.version)
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
    response.headers["ETag"] = _etag(item.version)
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
    expected = _parse_if_match(request.headers.get("if-match"))
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
    response.headers["ETag"] = _etag(out.version)
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
    response.headers["ETag"] = _etag(out.version)
    return out
