"""`POST /internal/schedule/tick` — lịch tới hạn, quyền `run_as`, và chuỗi bước.

Lịch nằm trong `item.definition` (`PipelineDefinition.schedule`), KHÔNG trong
một bảng riêng — bảng `pipeline` của migration 0008 đã bị 0009 bỏ. Mọi phép kiểm
dưới đây vì vậy dựng lịch bằng cách ghi một hàng `item` kiểu `pipeline`, và đó
chính là điều làm chúng chứng minh được: một bản cài đặt còn đọc bảng cũ sẽ
không thấy lịch nào tới hạn.

**"Tới hạn" được dựng bằng `updated_at`, không bằng một cột `next_run_at`.** Neo
của một pipeline chưa từng chạy là `item.updated_at`; nhịp kế tiếp là
`next_tick(cron, timezone, neo)`. Nên mọi phép kiểm ở đây đặt `updated_at` LÙI
LẠI một khoảng và dùng cron mỗi-phút — mốc `scheduled_for` vì vậy tính ra được
CHÍNH XÁC và test không phụ thuộc vào lúc nó chạy.

`JobLauncher` thật bị thay bằng một double gắn vào `app.state.job_launcher` SAU
khi app đã dựng, đúng khuôn `test_ingest_api.py`. Ở đây double quan trọng vì
tính chất mạnh nhất của file này là một lời gọi KHÔNG xảy ra: một run bị đánh
`failed` vì `run_as` mất quyền mà VẪN phóng một Job là đúng con bug đáng sợ, và
chỉ launcher bắt được — cột `status` một mình thì không.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loom_api.models import (
    DEFAULT_TENANT_ID,
    IngestRun,
    Item,
    PipelineRun,
    PipelineStepRun,
    RoleAssignment,
)
from loom_core.item_definitions import ItemType
from loom_core.roles import Role

from .conftest import ApiWorld

pytestmark = pytest.mark.integration

TICK_HEADERS = {"X-Loom-Schedule-Secret": "dev-only-do-not-use-in-production"}

# Namespace PHẢI khớp `Settings.task_namespace` (mặc định `loom`): `envFrom`
# không vượt được namespace, nên `secret_name_for` từ chối một ref trỏ nơi khác
# — xem `ingest_service.py`. Một ref sai namespace ở đây sẽ làm bước hỏng vì lý
# do không liên quan gì tới thứ đang được kiểm.
K8S_REF = "k8s://loom/source-pg#pg-app-credentials"

EVERY_MINUTE = "* * * * *"


class _FakeLauncher:
    """Double cho `JobLauncher` — đúng hai phương thức của `JobLauncherLike`.

    Không `MagicMock`: một mock nhận mọi lời gọi kể cả sai tên tham số, nên nó
    không chứng minh được điều gì về cái đi vào launcher. `status()` ném
    `KeyError` vì không phép kiểm nào ở file này đối chiếu Job — một mặc định
    im lặng sẽ biến một đường đi không ai dựng thành một đường xanh.
    """

    def __init__(self) -> None:
        self.launched: list[uuid.UUID] = []

    def launch(
        self,
        run_id: uuid.UUID,
        secret_name: str,
        shared_secret_ref: tuple[str, str],
        cpu: str,
        memory: str,
    ) -> None:
        self.launched.append(run_id)

    def status(self, run_id: uuid.UUID) -> object:
        raise KeyError(run_id)


@pytest.fixture
async def session(api_world: ApiWorld) -> AsyncIterator[AsyncSession]:
    """Session RIÊNG của test, dựng trên cùng engine mà app dùng.

    `Database` chỉ lộ ra `session()` — một async context manager — chứ KHÔNG có
    `session_factory`, nên test phải tự dựng maker từ `api_world.engine`, đúng
    khuôn `test_ingest_reconcile.py`. Mọi dữ liệu dựng sẵn phải được COMMIT:
    app mở session riêng của nó và không thấy transaction của test.
    """
    maker = async_sessionmaker(api_world.engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture(autouse=True)
async def _clean_pipeline_rows(api_world: ApiWorld) -> AsyncIterator[None]:
    """Dọn `pipeline_run`/`pipeline_step_run` SAU mỗi phép kiểm, TRƯỚC `api_world`.

    Hai lý do, và cả hai đều là lỗi thật nếu thiếu:

    1. `pipeline_run.pipeline_id` có khoá ngoại tới `item.id`, và
       `pipeline_step_run.ingest_run_id` tới `ingest_run.id` — nên teardown của
       `api_world` (xoá item và ingest_run) VỠ nếu còn sót hàng ở đây. Fixture
       này phụ thuộc `api_world`, nên nó được dọn TRƯỚC.
    2. Tick quét MỌI hàng `item` kiểu pipeline trên cả database. Một run còn sót
       ở trạng thái `running` từ phép kiểm trước sẽ đội vào trần đồng thời toàn
       cục của phép kiểm sau.

    Xoá SẠCH hai bảng chứ không lọc: file này là chỗ duy nhất trong repo tạo ra
    hàng cho chúng.
    """
    yield
    maker = async_sessionmaker(api_world.engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(delete(PipelineStepRun))
        await s.execute(delete(PipelineRun))
        await s.commit()


def _launcher(world: ApiWorld) -> _FakeLauncher:
    launcher = _FakeLauncher()
    world.app.state.job_launcher = launcher
    return launcher


async def _insert_item(
    session: AsyncSession,
    world: ApiWorld,
    item_type: ItemType,
    definition: dict[str, Any],
    *,
    updated_at: datetime | None = None,
) -> uuid.UUID:
    """Một hàng `item` đã COMMIT, KHÔNG qua `ItemStore.create`.

    Đi qua store sẽ gọi `provision_warehouse` cho một lakehouse — tức là đòi một
    Lakekeeper thật, thứ file này không cần. Cùng lý do `test_ingest_api.py` ghi.
    """
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id=DEFAULT_TENANT_ID,
        workspace_id=world.ws_a,
        type=str(item_type),
        name=f"{item_type}-{item_id.hex[:8]}",
        display_name=str(item_type),
        definition=definition,
        definition_hash="x" * 64,
        created_by=world.user_id,
        updated_by=world.user_id,
    )
    if updated_at is not None:
        # Neo của một pipeline chưa từng chạy — xem docstring module.
        item.updated_at = updated_at
    session.add(item)
    await session.commit()
    return item_id


async def _lakehouse(session: AsyncSession, world: ApiWorld) -> uuid.UUID:
    return await _insert_item(session, world, ItemType.lakehouse, {"schema_version": 1})


async def _connection(session: AsyncSession, world: ApiWorld) -> uuid.UUID:
    return await _insert_item(
        session,
        world,
        ItemType.connection,
        {
            "schema_version": 1,
            "kind": "postgres",
            "host": "db.example.internal",
            "port": 5432,
            "database": "sales",
            "secret_ref": K8S_REF,
        },
    )


def _ingest_step(lakehouse_id: uuid.UUID, connection_id: uuid.UUID) -> dict[str, Any]:
    return {
        "type": "ingest",
        "ingest": {
            "lakehouse_id": str(lakehouse_id),
            "connection_id": str(connection_id),
            "stream": "public.orders",
            "mode": "full",
        },
    }


def _schedule(run_as: uuid.UUID | None, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "cron": EVERY_MINUTE,
        "timezone": "UTC",
        "run_as_user_id": None if run_as is None else str(run_as),
    }


def _minute_floor() -> datetime:
    """Bây giờ, cắt về đầu phút — để `next_tick` cho ra mốc TÍNH ĐƯỢC."""
    return datetime.now(UTC).replace(second=0, microsecond=0)


async def _runs_of(session: AsyncSession, pipeline_id: uuid.UUID) -> list[PipelineRun]:
    """Các run của pipeline này, ĐỌC LẠI TỪ DATABASE.

    `expunge_all()` trước khi hỏi là bắt buộc, không phải phòng xa: session của
    test dùng `expire_on_commit=False` và chính nó đã chèn vài hàng ở đây, nên
    identity map giữ bản CŨ — còn thứ cần đọc là bản mà APP vừa ghi qua một
    connection khác.
    """
    session.expunge_all()
    return list(
        (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.pipeline_id == pipeline_id)
                .order_by(PipelineRun.scheduled_for)
            )
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------------------ lịch tới hạn


async def test_tick_requires_the_shared_secret(api_world: ApiWorld) -> None:
    """Không có header secret -> 401."""
    response = await api_world.client.post("/internal/schedule/tick")
    assert response.status_code == 401


async def test_a_due_schedule_creates_exactly_one_run(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Một lịch tới hạn ĐỌC TỪ DEFINITION -> đúng một `pipeline_run`, và Job lên.

    Không có bảng lịch nào ở đây: hàng duy nhất được dựng là một `item` kiểu
    `pipeline` mang `schedule` trong definition của nó.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    anchor = _minute_floor() - timedelta(hours=2)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [_ingest_step(lakehouse_id, connection_id)],
            "schedule": _schedule(api_world.user_id),
        },
        updated_at=anchor,
    )

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["schedules_processed"] >= 1
    assert body["runs_started"] == 1

    runs = await _runs_of(session, pipeline_id)
    assert len(runs) == 1
    # Mốc nhịp là `next_tick` TỪ NEO, không phải "bây giờ" — cột `scheduled_for`
    # là thứ ràng buộc UNIQUE dựa vào, nên nó phải tính được chứ không xấp xỉ.
    assert runs[0].scheduled_for == anchor + timedelta(minutes=1)
    assert runs[0].status == "running"
    assert len(launcher.launched) == 1


async def test_two_concurrent_ticks_create_exactly_one_run(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Ràng buộc UNIQUE thay cho advisory lock — hai tick song song, MỘT run, MỘT Job.

    Hai request THẬT SỰ song song (`asyncio.gather`): hai lời gọi tuần tự xanh y
    nguyên với một bản cài đặt không có ràng buộc nào cả. Và đếm cả Job: ràng
    buộc UNIQUE chặn được hàng thứ hai chứ không chặn được pod thứ hai — chỉ
    `RETURNING` trên `ON CONFLICT DO NOTHING` làm được điều đó.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    anchor = _minute_floor() - timedelta(hours=2)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [_ingest_step(lakehouse_id, connection_id)],
            "schedule": _schedule(api_world.user_id),
        },
        updated_at=anchor,
    )

    results = await asyncio.gather(
        api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS),
        api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS),
    )
    assert all(r.status_code == 200 for r in results)

    count = (
        await session.execute(
            select(func.count(PipelineRun.id)).where(
                PipelineRun.pipeline_id == pipeline_id,
                PipelineRun.scheduled_for == anchor + timedelta(minutes=1),
            )
        )
    ).scalar()
    assert count == 1
    assert len(launcher.launched) == 1, "một nhịp cron chỉ được phóng MỘT Job"


async def test_a_pipeline_still_running_records_a_skipped_row(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Một run đang chạy -> tick ghi HÀNG `skipped` kèm lý do, không phải một dòng log."""
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    anchor = _minute_floor() - timedelta(hours=1)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [_ingest_step(lakehouse_id, connection_id)],
            "schedule": _schedule(api_world.user_id),
        },
        updated_at=anchor - timedelta(hours=1),
    )
    # Run ĐANG CHẠY của nhịp trước. Nó cũng là NEO: nhịp tới hạn kế tiếp là
    # `anchor + 1 phút`, không phải "bây giờ".
    session.add(
        PipelineRun(
            id=uuid.uuid4(),
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            scheduled_for=anchor,
            status="running",
            run_as_user_id=api_world.user_id,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200
    assert response.json()["runs_skipped"] >= 1

    skipped = (
        (
            await session.execute(
                select(PipelineRun).where(
                    PipelineRun.pipeline_id == pipeline_id,
                    PipelineRun.scheduled_for == anchor + timedelta(minutes=1),
                    PipelineRun.status == "skipped",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(skipped) == 1
    assert skipped[0].skip_reason is not None
    assert launcher.launched == [], "một nhịp bị bỏ không được phóng Job nào"


async def test_a_disabled_schedule_is_not_started(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """`enabled: false` — tới hạn theo cron nhưng KHÔNG chạy.

    Neo và cron y hệt phép kiểm "tới hạn" ở trên, nên thứ duy nhất khác biệt là
    cái cờ. Nếu tick bỏ qua `enabled`, phép kiểm này đỏ ngay.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [_ingest_step(lakehouse_id, connection_id)],
            "schedule": _schedule(api_world.user_id, enabled=False),
        },
        updated_at=_minute_floor() - timedelta(hours=2),
    )

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200

    assert await _runs_of(session, pipeline_id) == []
    assert launcher.launched == []


async def test_a_pipeline_without_a_schedule_is_not_started(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Definition KHÔNG có khoá `schedule` — pipeline chạy tay, tick không đụng tới.

    `schedule` là tuỳ chọn (`ScheduleDefinition | None`), nên đây là hình dạng
    của MỌI pipeline chưa ai đặt lịch. Tick coi nó như một lịch tắt.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [_ingest_step(lakehouse_id, connection_id)]},
        updated_at=_minute_floor() - timedelta(hours=2),
    )

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200

    assert await _runs_of(session, pipeline_id) == []
    assert launcher.launched == []


# ------------------------------------------------------------------ quyền run_as


async def test_a_run_as_who_lost_permission_fails_loudly(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Kiểm ở MỖI nhịp, không phải một lần lúc bật lịch. Người đó nghỉ việc hay
    bị thu quyền thì run phải HỎNG TO TIẾNG — không lặng lẽ chạy tiếp bằng quyền
    đã cấp từ sáu tháng trước."""
    launcher = _launcher(api_world)

    # `bob` là người mà LỊCH chạy dưới danh nghĩa. Người tạo/sửa item vẫn là
    # `api_world.user_id`, và người đó CÓ quyền — nên một bản cài đặt lấy
    # `created_by`/`updated_by` sẽ chạy trót lọt và phép kiểm này đỏ.
    bob = await api_world.make_user("bob")
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor, user=bob)
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    anchor = _minute_floor() - timedelta(hours=2)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [_ingest_step(lakehouse_id, connection_id)],
            "schedule": _schedule(bob),
        },
        updated_at=anchor,
    )

    # ... RỒI thu quyền của bob. Lịch đã bật từ trước với một người có quyền;
    # cái thay đổi là hôm nay bob không còn quyền nữa.
    await session.execute(delete(RoleAssignment).where(RoleAssignment.principal_user_id == bob))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200
    assert response.json()["runs_failed"] == 1

    runs = await _runs_of(session, pipeline_id)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "failed"
    assert run.error is not None
    assert "permission" in run.error.lower()
    assert launcher.launched == [], "không được phóng Job nào"
    # Và KHÔNG có bước nào được hiện thực hoá: một run hỏng ở cổng quyền chưa
    # bao giờ bắt đầu, nên nó không để lại nửa cái chuỗi cho ai dọn.
    steps = (
        await session.execute(
            select(func.count(PipelineStepRun.id)).where(PipelineStepRun.pipeline_run_id == run.id)
        )
    ).scalar()
    assert steps == 0
    # Và không có hàng `ingest_run` nào: `launched == []` một mình không nói gì
    # về Postgres — một bản cài đặt tạo hàng TRƯỚC cổng quyền vẫn không phóng
    # Job nào (xem cùng lập luận ở `test_ingest_api.py`).
    ingest_runs = (
        await session.execute(
            select(func.count(IngestRun.id)).where(IngestRun.lakehouse_id == lakehouse_id)
        )
    ).scalar()
    assert ingest_runs == 0


async def test_the_run_records_the_schedules_principal_not_the_editor(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """`run_as_user_id` của run LÀ người mà `ScheduleDefinition` nêu tên.

    Đây là tính chất mà chủ dự án chốt, phát biểu THẲNG: theo
    `created_by`/`updated_by` thì quyền TRÔI mỗi lần sửa — một admin sửa chính
    tả trong pipeline của một contributor là pipeline đó chạy bằng quyền admin,
    không ai định thế và không ai thấy. `bob` KHÔNG phải người tạo item, nên
    một bản cài đặt lấy `created_by` sẽ ghi sai id ở đây.
    """
    _launcher(api_world)

    bob = await api_world.make_user("bob")
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor, user=bob)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [_ingest_step(lakehouse_id, connection_id)],
            "schedule": _schedule(bob),
        },
        updated_at=_minute_floor() - timedelta(hours=2),
    )
    item = (await session.execute(select(Item).where(Item.id == pipeline_id))).scalar_one()
    assert item.created_by == api_world.user_id != bob, "tiền đề: người tạo KHÁC run_as"

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200

    runs = await _runs_of(session, pipeline_id)
    assert len(runs) == 1
    assert runs[0].run_as_user_id == bob
    assert runs[0].status == "running", "bob có quyền, nên run phải chạy chứ không hỏng"


# ----------------------------------------------------------------- chuỗi bước


async def test_the_next_step_starts_only_after_the_previous_succeeded(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Bước tiếp theo chỉ bắt đầu khi bước trước đã THÀNH CÔNG.

    Không có `schedule` trong definition: phép kiểm này nói về đường ĐẨY BƯỚC
    của một run đã chạy, nên một lịch tới hạn ở đây chỉ thêm một hàng nhiễu.
    """
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    step = _ingest_step(lakehouse_id, connection_id)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [step, step]},
    )

    run_id = uuid.uuid4()
    session.add(
        PipelineRun(
            id=run_id,
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            scheduled_for=_minute_floor(),
            status="running",
            run_as_user_id=api_world.user_id,
            started_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.flush()
    session.add(
        PipelineStepRun(
            id=uuid.uuid4(),
            pipeline_run_id=run_id,
            step_index=0,
            step_type="ingest",
            status="succeeded",
            started_at=datetime.now(UTC) - timedelta(minutes=2),
            finished_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    session.add(
        PipelineStepRun(
            id=uuid.uuid4(),
            pipeline_run_id=run_id,
            step_index=1,
            step_type="ingest",
            status="pending",
        )
    )
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200

    session.expunge_all()
    steps = (
        (
            await session.execute(
                select(PipelineStepRun)
                .where(PipelineStepRun.pipeline_run_id == run_id)
                .order_by(PipelineStepRun.step_index)
            )
        )
        .scalars()
        .all()
    )
    assert len(steps) == 2
    assert steps[0].status == "succeeded"
    assert steps[1].status == "running"
    assert steps[1].started_at is not None
    # Đẩy bước KHÔNG chỉ là đổi một chữ trong bảng: bước `ingest` phải có một
    # hàng `ingest_run` và một Job. Thiếu hai thứ đó thì "running" là một lời
    # nói dối và chuỗi đứng im mãi mãi.
    assert steps[1].ingest_run_id is not None
    assert launcher.launched == [steps[1].ingest_run_id]


async def test_a_failed_step_stops_the_chain_and_fails_the_run(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Bước hỏng -> DỪNG chuỗi, không chạy bước tiếp theo.

    Một SQL dựng silver chạy trên bronze thiếu dữ liệu cho ra một bảng SAI mà
    không lỗi nào báo ra — đúng dạng hỏng không ai phát hiện.
    """
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    step = _ingest_step(lakehouse_id, connection_id)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [step, step]},
    )

    run_id = uuid.uuid4()
    session.add(
        PipelineRun(
            id=run_id,
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            scheduled_for=_minute_floor(),
            status="running",
            run_as_user_id=api_world.user_id,
            started_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.flush()
    session.add(
        PipelineStepRun(
            id=uuid.uuid4(),
            pipeline_run_id=run_id,
            step_index=0,
            step_type="ingest",
            status="failed",
            started_at=datetime.now(UTC) - timedelta(minutes=2),
            finished_at=datetime.now(UTC) - timedelta(minutes=1),
            error="connection refused",
        )
    )
    session.add(
        PipelineStepRun(
            id=uuid.uuid4(),
            pipeline_run_id=run_id,
            step_index=1,
            step_type="ingest",
            status="pending",
        )
    )
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200

    session.expunge_all()
    step_one = (
        await session.execute(
            select(PipelineStepRun).where(
                PipelineStepRun.pipeline_run_id == run_id,
                PipelineStepRun.step_index == 1,
            )
        )
    ).scalar_one()
    run = (await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one()

    assert step_one.status == "pending"
    assert run.status == "failed"
    assert launcher.launched == [], "chuỗi đã dừng, không được phóng Job nào"
