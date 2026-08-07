"""`POST /workspaces/{id}/items` với `type=lakehouse` phải cấp phát warehouse
— qua ĐÚNG đường HTTP, không chỉ qua `ItemStore.create` trực tiếp.

Khác `test_warehouse_provisioning_lifecycle.py` (gọi thẳng `ItemStore.create`
với một `provision` tự tay lắp): bài test ở đây đi qua router thật
(`loom_api.routers.items.create_item`), nên nó CŨNG canh được một lỗi chỉ có
thể xảy ra ở tầng router — ví dụ ai đó nối nhầm `body.name` (tên hiển thị,
đổi được) vào chỗ lẽ ra phải là `item.id` (không đổi được) khi dựng
`functools.partial(provision_warehouse, ...)`. Lỗi đó sống ở router, không
sống ở `ItemStore`/`warehouse_provisioning`, nên chỉ một test đi qua HTTP mới
thấy được nó.

Không cần Lakekeeper/MinIO thật: `ensure_bootstrapped`/`create_warehouse` bị
thay bằng con giả, giống `test_warehouse_provisioning_lifecycle.py`.
"""

import pytest

from loom_core.roles import Role

pytestmark = pytest.mark.integration


@pytest.fixture
async def contributor(api_world):
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    return api_world


@pytest.fixture
def warehouse_calls(monkeypatch):
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
        calls.append({"fn": "create_warehouse", "name": name, "key_prefix": key_prefix})
        return "fake-warehouse-id"

    monkeypatch.setattr(
        "loom_api.warehouse_provisioning.ensure_bootstrapped", fake_ensure_bootstrapped
    )
    monkeypatch.setattr("loom_api.warehouse_provisioning.create_warehouse", fake_create_warehouse)
    return calls


async def test_creating_a_lakehouse_over_http_names_the_warehouse_after_the_item_id(
    contributor, warehouse_calls
):
    display_name = "Tên hiển thị dễ đổi sau này"
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "lakehouse",
            "name": "kho-du-lieu-http",
            "display_name": display_name,
            "definition": {"schema_version": 1},
        },
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]

    assert [c["fn"] for c in warehouse_calls] == ["ensure_bootstrapped", "create_warehouse"]
    created = warehouse_calls[1]
    assert created["name"] == item_id
    assert created["name"] != display_name
    assert created["name"] != "kho-du-lieu-http"


async def test_creating_a_non_lakehouse_item_over_http_never_calls_lakekeeper(
    contributor, warehouse_calls
):
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "sql_script",
            "name": "truy-van-http",
            "display_name": "Truy vấn",
            "definition": {"schema_version": 1, "sql": "SELECT 1"},
        },
    )
    assert r.status_code == 201, r.text
    assert warehouse_calls == []
