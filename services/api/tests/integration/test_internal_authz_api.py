"""`POST /internal/authz/items` qua đúng đường HTTP.

Khác `test_effective_roles_for_items.py` (gọi thẳng `PermissionService`): ở đây
request đi qua router thật, và câu hỏi là "handler có tự thêm một nhánh làm lộ
sự tồn tại không" — thứ mà một test ở tầng service không thấy được, vì
`effective_roles_for_items` tự nó đã không phân biệt hai trường hợp.

`api_world` cho app THẬT (transport ASGI, không cookie phiên nào áp dụng ở đây
vì router `internal` không đọc cookie) chạy trên schema đã migrate.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, Item
from loom_core.roles import Role

from .conftest import ApiWorld

pytestmark = pytest.mark.integration


async def _insert_item(world: ApiWorld, workspace_id: uuid.UUID, name: str) -> uuid.UUID:
    """Chèn thẳng qua DB, không qua `POST /items`: tạo item thật mà principal
    của `api_world` KHÔNG có quyền gì trên nó, để có một item TỒN TẠI nhưng
    không thấy được — phân biệt với một item không tồn tại chút nào."""
    item_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type="sql_script",
                name=name,
                display_name=name,
                definition={"schema_version": 1, "sql": ""},
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


def _body(world: ApiWorld, item_ids: list[uuid.UUID]) -> dict[str, object]:
    return {
        "principal": {
            "user_id": str(world.principal.user_id),
            "subject": world.principal.subject,
            "email": world.principal.email,
            "display_name": world.principal.display_name,
            "groups": list(world.principal.groups),
        },
        "item_ids": [str(i) for i in item_ids],
    }


async def test_missing_item_and_forbidden_item_are_indistinguishable(
    api_world: ApiWorld,
) -> None:
    """Đây là phép canh rò rỉ sự tồn tại. `ws_b` không có assignment nào cho
    principal của `api_world`, nên `forbidden` TỒN TẠI thật nhưng principal
    không thấy được nó — trong khi `missing` không tồn tại chút nào. Cả hai
    phải ra CÙNG một `null`, cùng status 200, không có cách nào phân biệt.
    """
    missing = uuid.uuid4()
    forbidden = await _insert_item(api_world, api_world.ws_b, "khong-thay-duoc")

    r = await api_world.client.post(
        "/internal/authz/items", json=_body(api_world, [missing, forbidden])
    )

    assert r.status_code == 200
    assert r.json() == {"roles": {str(missing): None, str(forbidden): None}}


async def test_a_role_the_caller_actually_has_comes_through(api_world: ApiWorld) -> None:
    item_id = await _insert_item(api_world, api_world.ws_a, "thay-duoc")
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)

    r = await api_world.client.post("/internal/authz/items", json=_body(api_world, [item_id]))

    assert r.status_code == 200
    assert r.json() == {"roles": {str(item_id): "contributor"}}


async def test_the_endpoint_is_not_reachable_under_api_v1(api_world: ApiWorld) -> None:
    """Chốt chống-hồi-quy nhỏ: nếu ai đó lỡ gắn router `internal` dưới
    `/api/v1` (đúng lỗi mà `test_internal_route_boundary.py` canh ở tầng cấu
    trúc route), request qua đường cũ vẫn phải 404 — router chỉ sống ở
    `/internal`."""
    r = await api_world.client.post(
        "/api/v1/internal/authz/items", json=_body(api_world, [uuid.uuid4()])
    )
    assert r.status_code == 404
