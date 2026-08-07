"""Ba trong năm chứng minh đỏ của task vòng đời warehouse — KHÔNG cần
container Lakekeeper/MinIO thật.

`ItemStore.create` chỉ biết gọi một callback đồng bộ (`provision`) — nó không
biết callback đó nói chuyện với Lakekeeper. Ba phép kiểm dưới đây lợi dụng
đúng ranh giới đó: thay `loom_api.warehouse_provisioning.ensure_bootstrapped`/
`create_warehouse` bằng một con giả (không I/O, không cần Docker) để kiểm
HÀNH VI của `provision_warehouse` + `ItemStore.create` phối hợp với nhau,
không phải kiểm Lakekeeper thật hoạt động đúng (đã có
`packages/icebergkit/tests/integration/test_lakehouse.py` cho việc đó).

Phép kiểm thứ tư (soft-delete không xoá warehouse, và warehouse còn dùng
được) cần Lakekeeper THẬT để khẳng định "còn dùng được" có ý nghĩa — xem
`test_lakehouse_warehouse_e2e.py`.
"""

import uuid
from functools import partial

import pytest
from sqlalchemy import func, select

from loom_api.item_store import ItemStore
from loom_api.models import Item
from loom_api.warehouse_provisioning import provision_warehouse
from loom_core.config import Settings
from loom_core.item_definitions import ItemType
from loom_storage.credentials import prefix_for_lakehouse

pytestmark = pytest.mark.integration

# Payload hợp lệ cho các loại item KHÔNG phải lakehouse — cần đủ trường bắt
# buộc để `parse_definition` không đỏ vì lý do khác với thứ test này kiểm.
_NON_LAKEHOUSE_DEFINITIONS: dict[ItemType, dict[str, object]] = {
    ItemType.connection: {
        "schema_version": 1,
        "kind": "postgres",
        "host": "db.local",
        "port": 5432,
        "secret_ref": "vault://loom/db#password",
    },
    ItemType.pipeline: {"schema_version": 1, "nodes": [], "edges": []},
    ItemType.sql_script: {"schema_version": 1, "sql": ""},
}


def _settings() -> Settings:
    # Giá trị mặc định là đủ: hai hàm chạm mạng bị thay bằng con giả bên dưới
    # nên KHÔNG request nào thật sự rời tiến trình test.
    return Settings()


# ------------------------------------------------- Chứng minh đỏ 2: thứ tự tạo


async def test_failed_warehouse_provisioning_creates_no_item_row(rbac_fixture, contributor_bob):
    """Nếu Lakekeeper hỏng, `create()` phải KHÔNG để lại hàng `item` nào —
    không chỉ ném lỗi ra ngoài. Đảo thứ tự (tạo hàng item TRƯỚC khi gọi
    `provision`) làm phép kiểm này ĐỎ dù response vẫn là lỗi, vì lúc đó
    `select(...)` bên dưới (autoflush) sẽ thấy đúng một hàng đang chờ commit."""
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")

    def boom(item_id: uuid.UUID) -> None:
        raise RuntimeError("lakekeeper khong the ket noi")

    with pytest.raises(RuntimeError, match="lakekeeper khong the ket noi"):
        await store.create(
            workspace_id=f.ws_a,
            item_type=ItemType.lakehouse,
            name="kho-hong",
            display_name="Kho hỏng",
            definition={"schema_version": 1},
            provision=boom,
        )

    count = (
        await f.session.execute(
            select(func.count())
            .select_from(Item)
            .where(Item.workspace_id == f.ws_a, Item.name == "kho-hong")
        )
    ).scalar_one()
    assert count == 0, "warehouse hỏng nhưng vẫn có một hàng item — thứ tự đã bị đảo"


async def test_successful_provisioning_still_creates_the_item(rbac_fixture, contributor_bob):
    """Đối chứng của phép trên: `provision` chạy xong không lỗi thì item vẫn
    được tạo bình thường — phép kiểm trên không phải vô tình chặn MỌI item."""
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    seen: list[uuid.UUID] = []
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.lakehouse,
        name="kho-on",
        display_name="Kho ổn",
        definition={"schema_version": 1},
        provision=seen.append,
    )
    assert seen == [item.id]
    count = (
        await f.session.execute(select(func.count()).select_from(Item).where(Item.id == item.id))
    ).scalar_one()
    assert count == 1


# ------------------------------------------- Chứng minh đỏ 3: chỉ lakehouse mới gọi Lakekeeper


@pytest.mark.parametrize("item_type", sorted(_NON_LAKEHOUSE_DEFINITIONS, key=str))
async def test_non_lakehouse_types_never_call_lakekeeper(
    rbac_fixture, contributor_bob, monkeypatch, item_type
):
    calls: list[str] = []
    monkeypatch.setattr(
        "loom_api.warehouse_provisioning.ensure_bootstrapped",
        lambda *a, **k: calls.append("ensure_bootstrapped"),
    )
    monkeypatch.setattr(
        "loom_api.warehouse_provisioning.create_warehouse",
        lambda *a, **k: calls.append("create_warehouse") or "fake-id",
    )
    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    settings = _settings()
    await store.create(
        workspace_id=f.ws_a,
        item_type=item_type,
        name=f"item-{item_type.value}",
        display_name="x",
        definition=_NON_LAKEHOUSE_DEFINITIONS[item_type],
        provision=partial(provision_warehouse, settings, item_type=item_type, workspace_id=f.ws_a),
    )
    assert calls == [], f"{item_type} không phải lakehouse nhưng đã gọi Lakekeeper: {calls}"


# ------------------------------------------- Chứng minh đỏ 4: tên = str(item.id), key-prefix đúng


async def test_lakehouse_warehouse_named_after_item_id_not_item_name(
    rbac_fixture, contributor_bob, monkeypatch
):
    calls: list[dict[str, object]] = []

    def fake_ensure_bootstrapped(management_url: str) -> None:
        calls.append({"fn": "ensure_bootstrapped", "management_url": management_url})

    def fake_create_warehouse(
        management_url: str,
        *,
        name: str,
        bucket: str,
        key_prefix: str,
        s3_endpoint: str,
        access_key: str,
        secret_key: str,
    ) -> str:
        calls.append(
            {
                "fn": "create_warehouse",
                "name": name,
                "bucket": bucket,
                "key_prefix": key_prefix,
            }
        )
        return "fake-warehouse-id"

    monkeypatch.setattr(
        "loom_api.warehouse_provisioning.ensure_bootstrapped", fake_ensure_bootstrapped
    )
    monkeypatch.setattr("loom_api.warehouse_provisioning.create_warehouse", fake_create_warehouse)

    f = rbac_fixture
    store = ItemStore(f.session, f.principal_bob, request_id="r1")
    settings = _settings()
    item = await store.create(
        workspace_id=f.ws_a,
        item_type=ItemType.lakehouse,
        name="kho-dat-ten-de-doi",
        display_name="Kho đặt tên dễ đổi",
        definition={"schema_version": 1},
        provision=partial(
            provision_warehouse, settings, item_type=ItemType.lakehouse, workspace_id=f.ws_a
        ),
    )

    assert [c["fn"] for c in calls] == ["ensure_bootstrapped", "create_warehouse"]
    created = calls[1]
    # Đúng quy ước mà `loom_query.runner` giả định (`warehouse=str(lakehouse_id)`):
    # tên warehouse là ID, KHÔNG phải tên hiển thị — tên đổi được, id thì không.
    assert created["name"] == str(item.id)
    assert created["name"] != item.name
    assert created["key_prefix"] == prefix_for_lakehouse(f.ws_a, item.id)
    assert created["bucket"] == settings.storage_bucket
