"""Cổng quyền của đường nạp — `POST /api/v1/lakehouses/{id}/ingest`.

GHI đòi `contributor`, không phải `viewer`. Đây CHÍNH LÀ lỗ mà Giai đoạn 2c
phát hiện ở CTAS: `ACTION_MATRIX` xếp `item.update` vào `contributor`, nhưng
cổng quyền chỉ đòi `viewer`, nên một viewer tạo được bảng. Nạp dữ liệu cũng là
GHI, và nó ghi vào lakehouse — nên `item.update` hỏi trên LAKEHOUSE.

Chạy qua HTTP THẬT (`api_world`, xem `conftest.py`) chứ không gọi thẳng hàm:
một cổng quyền đúng ở tầng dưới vẫn có thể bị bỏ qua ở tầng router, và chỉ
đường HTTP mới thấy điều đó. `JobLauncher` thật bị thay bằng một double ghi
lại lời gọi, gắn vào `app.state.job_launcher` SAU khi app đã dựng — đúng cách
`test_query_proxy_api.py` thay `app.state.query_http`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, IngestRun, Item
from loom_core.item_definitions import ItemType
from loom_core.roles import Role

from .conftest import ApiWorld

pytestmark = pytest.mark.integration

# `#password` là KHOÁ bên trong Secret, không phải mật khẩu — xem `SECRET_REF_RE`.
K8S_REF = "k8s://loom/source-pg#password"


@dataclass(frozen=True, slots=True)
class _LaunchCall:
    run_id: uuid.UUID
    secret_name: str
    shared_secret_ref: tuple[str, str]
    cpu: str
    memory: str


class _RecordingLauncher:
    """Double cho `JobLauncher` — CHỈ hiểu đúng chữ ký thật của `launch()`.

    Không `MagicMock`: một mock chấp nhận mọi lời gọi, kể cả sai tên tham số,
    nên nó không thể chứng minh điều bài test dưới đây nêu tên — rằng cái đi
    vào launcher là một cái TÊN Secret. Sai tham số ở đây là `TypeError` ngay,
    đúng như dùng nhầm `JobLauncher` thật.
    """

    def __init__(self) -> None:
        self.launched: list[_LaunchCall] = []

    def launch(
        self,
        run_id: uuid.UUID,
        secret_name: str,
        shared_secret_ref: tuple[str, str],
        cpu: str,
        memory: str,
    ) -> None:
        self.launched.append(_LaunchCall(run_id, secret_name, shared_secret_ref, cpu, memory))


async def _insert_item(
    world: ApiWorld,
    workspace_id: uuid.UUID,
    item_type: ItemType,
    name: str,
    definition: dict[str, Any],
) -> uuid.UUID:
    """Chèn thẳng một hàng `item` đã COMMIT — cùng khuôn `_insert_lakehouse` ở
    `test_query_proxy_api.py`, chỉ khác ở chỗ nhận `type`/`definition` để dựng
    được cả `lakehouse` lẫn `connection`.

    Không đi qua `ItemStore.create`: tạo một `lakehouse` qua đó sẽ gọi
    `provision_warehouse`, tức là đòi một Lakekeeper thật — thứ mà file này
    không cần và không nên phụ thuộc vào.
    """
    item_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type=str(item_type),
                name=name,
                display_name=name,
                definition=definition,
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


def _connection_definition(secret_ref: str = K8S_REF) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "postgres",
        "host": "db.example.internal",
        "port": 5432,
        "database": "sales",
        "secret_ref": secret_ref,
    }


async def _lakehouse(world: ApiWorld, workspace_id: uuid.UUID | None = None) -> uuid.UUID:
    return await _insert_item(
        world,
        workspace_id or world.ws_a,
        ItemType.lakehouse,
        f"lake-{uuid.uuid4().hex[:8]}",
        {"schema_version": 1},
    )


async def _connection(
    world: ApiWorld,
    workspace_id: uuid.UUID | None = None,
    secret_ref: str = K8S_REF,
) -> uuid.UUID:
    return await _insert_item(
        world,
        workspace_id or world.ws_a,
        ItemType.connection,
        f"conn-{uuid.uuid4().hex[:8]}",
        _connection_definition(secret_ref),
    )


def _launcher(world: ApiWorld) -> _RecordingLauncher:
    launcher = _RecordingLauncher()
    world.app.state.job_launcher = launcher
    return launcher


def _body(connection_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {
        "connection_id": str(connection_id),
        "stream": "public.orders",
        "mode": "full",
        **extra,
    }


async def _run_row(world: ApiWorld, run_id: uuid.UUID) -> IngestRun:
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    return row


async def test_a_viewer_cannot_start_an_ingest(api_world: ApiWorld) -> None:
    """403, và KHÔNG có Job nào được phóng.

    Chỉ khẳng định mã trạng thái là chưa đủ: một bản cài đặt phóng Job trước
    rồi mới kiểm quyền vẫn cho ra 403 cho người gọi trong khi pod nạp đã chạy
    và đang ghi vào lakehouse. `launcher.launched == []` là vế nói lên điều đó.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world)
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(connection_id)
    )

    assert response.status_code == 403, response.text
    assert launcher.launched == []


async def test_a_contributor_can_start_an_ingest(api_world: ApiWorld) -> None:
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world)
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(connection_id)
    )

    assert response.status_code == 202, response.text
    run_id = uuid.UUID(response.json()["run_id"])
    assert launcher.launched, "202 mà không phóng Job nào là một lời nói dối"
    assert launcher.launched[0].run_id == run_id, (
        "Job phải mang ĐÚNG run_id vừa trả về — tên Job là `ingest-{run_id}`, "
        "nên lệch ở đây nghĩa là không ai tra ngược được Job của một run"
    )


async def test_the_run_row_is_pending_and_carries_the_lakehouse_workspace(
    api_world: ApiWorld,
) -> None:
    """`workspace_id` của hàng `ingest_run` lấy TỪ LAKEHOUSE, không từ client —
    client không có ô nào để khai nó, và đó là cố ý (xem `IngestStartRequest`).

    `status='pending'` là trạng thái ĐÚNG ngay sau khi phóng: nó nghĩa là "ý
    định đã bền trong Postgres, Job đã được yêu cầu". Chỉ pod nạp mới được
    chuyển nó sang `running` (Task 10), nên một bản cài đặt ghi thẳng
    `running` ở đây đang nói dối về một thứ nó không quan sát được.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world)
    _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest",
        json=_body(connection_id, stream="public.orders", mode="incremental"),
    )

    assert response.status_code == 202, response.text
    row = await _run_row(api_world, uuid.UUID(response.json()["run_id"]))
    assert row.workspace_id == api_world.ws_a
    assert row.lakehouse_id == lakehouse_id
    assert row.connection_id == connection_id
    assert (row.status, row.mode, row.stream) == ("pending", "incremental", "public.orders")


async def test_the_secret_value_never_reaches_the_launcher(api_world: ApiWorld) -> None:
    """`loom-api` chuyển TÊN Secret, không bao giờ đọc giá trị. Phép kiểm này
    khoá đúng lời hứa của Giai đoạn 1 mà `SECRET_REF_RE` bảo vệ ở đầu kia.

    Cả KHOÁ (`#password`) cũng không đi qua: `envFrom` chiếu TOÀN BỘ Secret
    vào pod, nên launcher chỉ cần cái tên. Một bản cài đặt chuyền thêm khoá
    (hay tệ hơn, đi đọc Secret để lấy giá trị) sẽ làm vế thứ hai đỏ.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world)
    launcher = _launcher(api_world)

    await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(connection_id)
    )

    (call,) = launcher.launched
    assert call.secret_name == "source-pg"
    assert "password" not in repr(call).lower()


async def test_an_unknown_mode_is_rejected(api_world: ApiWorld) -> None:
    """422 ở BIÊN, trước khi có hàng `ingest_run` nào — `mode` là một `Literal`
    trên request model, không phải một `if` trong handler."""
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world)
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest",
        json=_body(connection_id, mode="sync"),
    )

    assert response.status_code == 422, response.text
    assert launcher.launched == []


async def test_a_vault_secret_ref_is_rejected_at_local(api_world: ApiWorld) -> None:
    """Cụm local không tới được Vault (ràng buộc của chủ dự án). Một `vault://`
    ref ở local phải hỏng NGAY với thông báo rõ, không phải để Job khởi động
    rồi chết vì thiếu Secret — lỗi đó hiện ra ở chỗ không ai nghĩ tới."""
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world, secret_ref="vault://loom/source-pg#password")
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(connection_id)
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "Vault" in detail
    # Dạng thay thế phải có trong câu trả lời, không chỉ chữ "vault": thông báo
    # "không dùng được" chung chung nhúng cả `secret_ref` vào nên nó cũng chứa
    # "vault://" — xem `test_a_vault_ref_says_why_it_cannot_work_here`.
    assert "k8s://" in detail
    assert launcher.launched == []


async def test_a_connection_the_caller_cannot_see_is_not_found(api_world: ApiWorld) -> None:
    """Contributor trên lakehouse KHÔNG đủ để mượn một connection bất kỳ.

    Không có phép kiểm này, một contributor ở workspace A gõ được id của một
    connection ở workspace B mà họ không thấy, và Job sinh ra sẽ gắn vào một
    k8s Secret họ không có quyền gì với nó — credential của người khác, mượn
    qua một id đoán được. 404 (chứ không 403) là câu trả lời sẵn có của repo
    cho "bạn không thấy được thứ này" — xem `NotVisible` ở `permissions.py`.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world, workspace_id=api_world.ws_b)
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(connection_id)
    )

    assert response.status_code == 404, response.text
    assert launcher.launched == []


async def test_an_id_that_is_not_a_lakehouse_is_not_found(api_world: ApiWorld) -> None:
    """`lakehouse_id` phải là một item `lakehouse` đang sống. Một id
    `connection` (hay bất kỳ loại nào khác) trên đường dẫn cho ra 404, KHÔNG
    phải một run nạp vào một thứ không có bảng bronze nào."""
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    connection_id = await _connection(api_world)
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{connection_id}/ingest", json=_body(connection_id)
    )

    assert response.status_code == 404, response.text
    assert launcher.launched == []


async def test_an_id_that_is_not_a_connection_is_not_found(api_world: ApiWorld) -> None:
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    launcher = _launcher(api_world)

    response = await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(lakehouse_id)
    )

    assert response.status_code == 404, response.text
    assert launcher.launched == []


async def test_the_shared_secret_is_passed_by_name_and_key(api_world: ApiWorld) -> None:
    """Pod nạp nhận bí mật chia sẻ qua `secretKeyRef` — `loom-api` chuyển cặp
    (tên Secret, khoá) lấy từ Settings và không bao giờ đọc giá trị. Cùng lời
    hứa như secret nguồn, chỉ khác là cặp này do người vận hành cấu hình chứ
    không do người dùng nhập."""
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _lakehouse(api_world)
    connection_id = await _connection(api_world)
    launcher = _launcher(api_world)

    await api_world.client.post(
        f"/api/v1/lakehouses/{lakehouse_id}/ingest", json=_body(connection_id)
    )

    settings = api_world.app.state.settings
    (call,) = launcher.launched
    assert call.shared_secret_ref == (
        settings.task_shared_secret_name,
        settings.task_shared_secret_key,
    )
    assert (call.cpu, call.memory) == (settings.task_cpu, settings.task_memory)
