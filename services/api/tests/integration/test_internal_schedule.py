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

**`loom-query` cũng là một double** (`_FakeQueryService`, một
`httpx.MockTransport` gắn vào `app.state.query_http` — đúng khuôn
`test_query_proxy_api.py`). Điều đó có nghĩa và có giới hạn, ghi ra chứ không
giấu: những phép kiểm dưới đây chứng minh `loom-api` nộp ĐÚNG thân request (nhất
là `principal` — thứ `loom-query` TIN, xem `_start_sql_step`), ghi lại
`query_id`, và biến từng trạng thái trả về thành đúng nước đi tiếp theo của
chuỗi. Chúng KHÔNG chứng minh câu SQL chạy được, hay `run_gate` của `loom-query`
chấp nhận principal đó — thứ đó thuộc `make smoke` trên một cụm thật.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from loom_api.jobs import JobStatus
from loom_api.models import (
    DEFAULT_TENANT_ID,
    IngestRun,
    Item,
    PipelineRun,
    PipelineStepRun,
    RoleAssignment,
    Workspace,
)
from loom_core.internal_auth import QUERY_SHARED_SECRET_HEADER
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
    `KeyError` cho một run mà phép kiểm CHƯA khai trạng thái Job — im lặng trả
    về một mặc định sẽ biến một phép kiểm thiếu tiền đề thành một phép kiểm xanh
    về một tình huống không ai dựng. Cùng lý do `test_ingest_reconcile.py` ghi.

    `status_calls` là vế "một lời gọi KHÔNG xảy ra": khi hàng `ingest_run` đã ở
    trạng thái cuối, không ai được hỏi Kubernetes nữa (Job đã bị TTL dọn sau một
    giờ, và câu trả lời `exists=False` sẽ biến `succeeded` thành `failed`).
    """

    def __init__(self) -> None:
        self.launched: list[uuid.UUID] = []
        self.status_calls: list[uuid.UUID] = []
        self._statuses: dict[uuid.UUID, JobStatus] = {}
        # Mặc định cho những run mà phép kiểm KHÔNG biết trước id. Để `None`
        # (nghĩa là `KeyError`) trừ khi phép kiểm tự bật lên: xem
        # `test_two_concurrent_ticks_create_exactly_one_run` cho trường hợp
        # duy nhất hôm nay cần nó, và lý do nó là hành vi THẬT chứ không phải
        # một chỗ vá cho test.
        self.default_status: JobStatus | None = None

    def set_status(self, run_id: uuid.UUID, status: JobStatus) -> None:
        self._statuses[run_id] = status

    def launch(
        self,
        run_id: uuid.UUID,
        secret_name: str,
        shared_secret_ref: tuple[str, str],
        cpu: str,
        memory: str,
    ) -> None:
        self.launched.append(run_id)

    def status(self, run_id: uuid.UUID) -> JobStatus:
        self.status_calls.append(run_id)
        if run_id in self._statuses:
            return self._statuses[run_id]
        if self.default_status is None:
            raise KeyError(run_id)
        return self.default_status


class _FakeQueryService:
    """`loom-query` giả: ghi lại mọi request, và trả trạng thái do test đặt.

    Trạng thái theo TỪNG `query_id` chứ không một biến chung: một chuỗi
    `ingest → sql` chỉ có một query, nhưng một pipeline hai bước SQL thì có hai,
    và một double trả cùng một câu cho cả hai sẽ làm phép kiểm "bước sau chỉ
    chạy khi bước trước xong" mù đúng chỗ nó đặt tên.

    `GET` một `query_id` chưa được khai trả 404 — ĐÚNG thứ `loom-query` thật trả
    về cho một id nó không biết (bộ nhớ của nó chỉ nằm trong RAM, xem
    `loom_query.store`), nên đây không phải một mặc định bịa ra.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[httpx.Request, bytes]] = []
        self.submitted: list[str] = []
        self._statuses: dict[str, dict[str, Any]] = {}
        # Đáp ứng của `POST /query`. Test nào cần một `loom-query` từ chối câu
        # SQL thì thay bằng một 400.
        self.submit_response: httpx.Response | None = None

    def set_status(self, query_id: str, **payload: Any) -> None:
        self._statuses[query_id] = payload

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request, request.content))
        if request.method == "POST":
            if self.submit_response is not None:
                return self.submit_response
            query_id = str(uuid.uuid4())
            self.submitted.append(query_id)
            # Mặc định `running`: `loom-query` thật chạy câu SQL trong một task
            # nền và trả `202` NGAY, nên "vừa nộp xong" luôn là `running`.
            self._statuses[query_id] = {"status": "running"}
            return httpx.Response(202, json={"query_id": query_id})
        query_id = request.url.path.rsplit("/", 1)[-1]
        payload = self._statuses.get(query_id)
        if payload is None:
            return httpx.Response(404, json={"detail": "no query with this id"})
        return httpx.Response(200, json=payload)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(content) for _, content in self.requests if content]


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


def _query_service(world: ApiWorld) -> _FakeQueryService:
    service = _FakeQueryService()
    world.app.state.query_http = httpx.AsyncClient(transport=httpx.MockTransport(service.handle))
    return service


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


def _sql_step(
    lakehouse_id: uuid.UUID, sql: str = "CREATE TABLE silver.orders AS SELECT 1"
) -> dict[str, Any]:
    return {"type": "sql", "sql": {"lakehouse_id": str(lakehouse_id), "sql": sql}}


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


async def _running_run(
    session: AsyncSession,
    world: ApiWorld,
    pipeline_id: uuid.UUID,
    *,
    run_as: uuid.UUID | None = None,
) -> uuid.UUID:
    """Một `pipeline_run` ĐANG CHẠY, chưa commit — người gọi thêm bước rồi commit."""
    run_id = uuid.uuid4()
    session.add(
        PipelineRun(
            id=run_id,
            pipeline_id=pipeline_id,
            workspace_id=world.ws_a,
            scheduled_for=_minute_floor(),
            status="running",
            run_as_user_id=run_as or world.user_id,
            started_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.flush()
    return run_id


async def _ingest_run(
    session: AsyncSession,
    world: ApiWorld,
    lakehouse_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
) -> uuid.UUID:
    """Một hàng `ingest_run` — nguồn sự thật của một bước nạp (spec mục 3)."""
    run_id = uuid.uuid4()
    session.add(
        IngestRun(
            id=run_id,
            lakehouse_id=lakehouse_id,
            connection_id=connection_id,
            workspace_id=world.ws_a,
            stream="public.orders",
            mode="full",
            status=status,
            error=error,
        )
    )
    await session.flush()
    return run_id


def _step(
    run_id: uuid.UUID,
    index: int,
    step_type: str,
    status: str,
    *,
    ingest_run_id: uuid.UUID | None = None,
    query_id: str | None = None,
    error: str | None = None,
) -> PipelineStepRun:
    started = datetime.now(UTC) - timedelta(minutes=2) if status != "pending" else None
    finished = (
        datetime.now(UTC) - timedelta(minutes=1) if status in {"succeeded", "failed"} else None
    )
    return PipelineStepRun(
        id=uuid.uuid4(),
        pipeline_run_id=run_id,
        step_index=index,
        step_type=step_type,
        status=status,
        ingest_run_id=ingest_run_id,
        query_id=query_id,
        error=error,
        started_at=started,
        finished_at=finished,
    )


async def _steps_of(session: AsyncSession, run_id: uuid.UUID) -> list[PipelineStepRun]:
    """Các bước của một run, ĐỌC LẠI TỪ DATABASE — xem `_runs_of` cho lý do `expunge_all`."""
    session.expunge_all()
    return list(
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


async def _run_row(session: AsyncSession, run_id: uuid.UUID) -> PipelineRun:
    session.expunge_all()
    return (await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one()


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

    `default_status` phải bật ở ĐÂY và chỉ ở đây, và lý do là một tính chất
    THẬT của hệ thống chứ không phải một chỗ vá: tick THUA cuộc đua vẫn chạy
    nửa "đẩy run đang dở" của nó, và nó thấy run mà tick THẮNG vừa tạo. Nó
    không có run đó trong `started_now` của mình (bộ đó chỉ biết những gì
    CHÍNH nó khởi động), nên nó đối chiếu bước nạp và hỏi Kubernetes thật.
    Đúng điều sẽ xảy ra với hai replica scheduler; câu trả lời "Job đang chạy"
    là câu trả lời đúng cho một Job vừa được tạo.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)
    launcher.default_status = JobStatus(exists=True, active=1)

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


async def test_a_pipeline_in_a_deleted_workspace_is_not_started(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Xoá mềm WORKSPACE phải dừng lịch bên trong nó — kể cả khi hàng `item` vẫn `active`.

    **Đây không phải một trường hợp giả định; nó đã xảy ra trên một cụm sống.**
    `WorkspaceStore.soft_delete` chỉ đặt `workspace.state = 'deleted'` và CỐ Ý
    không chạm `item.state` của các item bên trong (không có cascade). Trước bản
    sửa, `_process_tick` chỉ lọc `Item.state == ACTIVE`, nên một pipeline trong
    một workspace đã xoá vẫn tới hạn mỗi nhịp và vẫn phóng một Job nạp THẬT —
    mãi mãi, trong một workspace mà API không còn đường nào đi tới để tắt nó.
    `make smoke` phép 15 đo được đúng điều đó: ba pipeline bỏ lại từ ba lần chạy
    vẫn sinh Job sau khi workspace của chúng đã biến mất.

    Neo và cron y hệt phép kiểm "tới hạn" ở trên, nên thứ DUY NHẤT khác biệt là
    trạng thái của workspace. Chứng minh đỏ bằng cách bỏ vế `Workspace.state ==
    ACTIVE` khỏi `_process_tick`.

    `launcher.launched == []` là vế quan trọng hơn `_runs_of(...) == []`: cái
    giá thật của lỗi này không phải một hàng thừa trong Postgres mà là một pod
    quay số ra một Postgres nguồn và ghi vào Iceberg mỗi phút.
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
            "schedule": _schedule(api_world.user_id),
        },
        updated_at=_minute_floor() - timedelta(hours=2),
    )
    # Xoá MỀM đúng như `WorkspaceStore.soft_delete` làm: chỉ cột `state`, và
    # KHÔNG đụng tới `item.state` — nếu phép kiểm cũng xoá item thì nó chứng
    # minh một thứ khác hẳn (và một thứ đã đúng từ trước).
    workspace = (
        await session.execute(select(Workspace).where(Workspace.id == api_world.ws_a))
    ).scalar_one()
    workspace.state = "deleted"
    await session.commit()
    try:
        response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
        assert response.status_code == 200
        assert response.json()["schedules_processed"] == 0

        assert await _runs_of(session, pipeline_id) == []
        assert launcher.launched == []
    finally:
        # Trả workspace về `active`: `api_world` xoá hàng của nó lúc teardown và
        # những phép kiểm sau dùng lại cùng fixture. Trong `finally` để một câu
        # khẳng định đỏ không kéo theo một lỗi teardown che mất nó.
        workspace.state = "active"
        await session.commit()


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


# ------------------------------------------------- đối chiếu bước nạp (ingest_run)


async def test_an_ingest_step_advances_once_its_ingest_run_succeeded(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Bước nạp XONG -> tick sau đóng bước đó và mở bước kế.

    Đây là nửa bị thiếu của chuỗi. Pod nạp báo `/complete` vào hàng `ingest_run`
    và KHÔNG biết gì về pipeline (3a có trước 3b), nên nếu tick không ĐỌC hàng
    đó thì `pipeline_step_run` nằm ở `running` vĩnh viễn và chuỗi không bao giờ
    đi tiếp — một pipeline chạy được nhưng không bao giờ xong.

    `status_calls == []` là vế thứ hai và nó không thừa: hàng `ingest_run` đã ở
    trạng thái CUỐI, nên không ai được hỏi Kubernetes về nó (Job đã bị TTL dọn
    sau một giờ, và `exists=False` sẽ lật `succeeded` thành `failed`). Cổng đó
    nằm trong `reconcile_ingest_run`, dùng chung với `GET /api/v1/ingest/{id}`.
    """
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    step = _ingest_step(lakehouse_id, connection_id)
    pipeline_id = await _insert_item(
        session, api_world, ItemType.pipeline, {"schema_version": 1, "steps": [step, step]}
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    ingest_id = await _ingest_run(
        session, api_world, lakehouse_id, connection_id, status="succeeded"
    )
    session.add(_step(run_id, 0, "ingest", "running", ingest_run_id=ingest_id))
    session.add(_step(run_id, 1, "ingest", "pending"))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "succeeded"
    assert steps[0].finished_at is not None
    assert steps[1].status == "running"
    # Bước kế phải THẬT SỰ chạy, không chỉ đổi một chữ trong bảng.
    assert steps[1].ingest_run_id is not None
    assert launcher.launched == [steps[1].ingest_run_id]
    assert launcher.status_calls == [], "một ingest_run đã kết thúc không được hỏi lại Kubernetes"


async def test_an_ingest_step_whose_job_vanished_fails_the_step_and_the_run(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Hàng `ingest_run` còn `running` nhưng Job đã biến mất -> bước hỏng, chuỗi dừng.

    Đây là đường đi qua `reconcile_ingest_run` THẬT (hàng chưa ở trạng thái cuối,
    nên nó hỏi Kubernetes). Không có nó, một pod bị OOMKill để lại bước ở
    `running` vĩnh viễn — cùng con bug mà `GET /api/v1/ingest/{id}` đã chữa cho
    đường tương tác, và lý do đường này gọi lại đúng hàm đó thay vì viết bản thứ
    hai.
    """
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    step = _ingest_step(lakehouse_id, connection_id)
    pipeline_id = await _insert_item(
        session, api_world, ItemType.pipeline, {"schema_version": 1, "steps": [step, step]}
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    ingest_id = await _ingest_run(session, api_world, lakehouse_id, connection_id, status="running")
    session.add(_step(run_id, 0, "ingest", "running", ingest_run_id=ingest_id))
    session.add(_step(run_id, 1, "ingest", "pending"))
    await session.commit()
    launcher.set_status(ingest_id, JobStatus(exists=False))

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "failed"
    assert steps[0].error is not None and "job" in steps[0].error.lower()
    assert steps[1].status == "pending", "chuỗi phải DỪNG, không chạy bước sau trên bronze thiếu"
    assert (await _run_row(session, run_id)).status == "failed"
    assert launcher.launched == []
    # Và hàng `ingest_run` cũng được đóng lại — trạng thái ở MỘT chỗ, không hai.
    ingest_row = (
        await session.execute(select(IngestRun).where(IngestRun.id == ingest_id))
    ).scalar_one()
    assert ingest_row.status == "failed"


async def test_a_step_still_running_is_never_started_a_second_time(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Bước 0 xong + bước 1 ĐANG CHẠY -> tick không đụng gì.

    Hình dạng này là cái bẫy của cách "tìm bước cuối đã kết thúc rồi mở bước
    kế": bước cuối đã kết thúc là bước 0, bước kế là bước 1, và bước 1 đang chạy
    — nên mỗi tick sẽ phóng thêm một hàng `ingest_run` và một Job nữa cho CÙNG
    một bước. Với chu kỳ tick vài chục giây, đó là hàng chục pod cùng ghi một
    bảng bronze trong một lần nạp dài.
    """
    launcher = _launcher(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    step = _ingest_step(lakehouse_id, connection_id)
    pipeline_id = await _insert_item(
        session, api_world, ItemType.pipeline, {"schema_version": 1, "steps": [step, step]}
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    done_id = await _ingest_run(session, api_world, lakehouse_id, connection_id, status="succeeded")
    live_id = await _ingest_run(session, api_world, lakehouse_id, connection_id, status="running")
    session.add(_step(run_id, 0, "ingest", "succeeded", ingest_run_id=done_id))
    session.add(_step(run_id, 1, "ingest", "running", ingest_run_id=live_id))
    await session.commit()
    launcher.set_status(live_id, JobStatus(exists=True, active=1))

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[1].status == "running"
    assert steps[1].ingest_run_id == live_id, "bước không được nối sang một ingest_run khác"
    assert launcher.launched == [], "một bước đang chạy không được phóng Job thứ hai"
    ingest_count = (
        await session.execute(
            select(func.count(IngestRun.id)).where(IngestRun.lakehouse_id == lakehouse_id)
        )
    ).scalar()
    assert ingest_count == 2, "đúng hai hàng ingest_run của chính phép kiểm này, không thêm"


# ----------------------------------------------------------------- bước SQL


async def test_a_sql_step_is_submitted_to_loom_query_as_the_run_as_principal(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Bước SQL đi sang `loom-query` DƯỚI DANH NGHĨA `run_as`, không ai khác.

    `loom-query` không có OIDC: nó nhận principal trong thân request và TIN nó
    (chỉ chặn bằng bí mật chia sẻ). Nên principal `loom-api` gửi CHÍNH LÀ thẩm
    quyền câu SQL chạy dưới. `bob` là `run_as`, còn người tạo item là
    `api_world.user_id` — một bản cài đặt gửi người tạo (hoặc principal của
    request tick, thứ không tồn tại ở đường này) sẽ đỏ ở đây.

    `workspace_id` phải là workspace THẬT tra từ lakehouse: nó là phạm vi phân
    giải tên bảng ba phần bên trong `loom-query`, và sai phạm vi là cổng quyền
    chạy trên sai tập item (xem docstring `routers/query.py`).
    """
    launcher = _launcher(api_world)
    query = _query_service(api_world)
    bob = await api_world.make_user("bob")
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor, user=bob)

    lakehouse_id = await _lakehouse(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [_sql_step(lakehouse_id, "SELECT 42")]},
    )

    run_id = await _running_run(session, api_world, pipeline_id, run_as=bob)
    session.add(_step(run_id, 0, "sql", "pending"))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    assert len(query.submitted) == 1, "đúng một lần nộp"
    submitted_request, _ = query.requests[-1]
    assert submitted_request.method == "POST"
    settings = api_world.app.state.settings
    assert submitted_request.headers[QUERY_SHARED_SECRET_HEADER] == settings.query_shared_secret
    body = query.bodies()[-1]
    assert body["principal"]["user_id"] == str(bob)
    assert body["sql"] == "SELECT 42"
    assert body["lakehouse_id"] == str(lakehouse_id)
    assert body["workspace_id"] == str(api_world.ws_a)

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "running"
    assert steps[0].started_at is not None
    # `query_id` là thứ DUY NHẤT nối bước này với công việc bên `loom-query`.
    # Thiếu nó, không tick nào hỏi được trạng thái và bước không bao giờ xong.
    assert steps[0].query_id == query.submitted[0]
    assert launcher.launched == [], "một bước SQL không phóng Job nạp nào"


async def test_a_sql_step_that_loom_query_refuses_fails_the_run(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """`loom-query` từ chối (403/400) -> bước hỏng NGAY kèm nguyên văn lý do.

    Thân phản hồi của `loom-query` đã đúng hình dạng người đọc cần; dịch lại nó
    thành một câu chung chung là vứt đúng dòng người vận hành phải đọc.
    """
    _launcher(api_world)
    query = _query_service(api_world)
    query.submit_response = httpx.Response(403, json={"detail": "principal cannot read ns.bronze"})

    lakehouse_id = await _lakehouse(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [_sql_step(lakehouse_id), _sql_step(lakehouse_id)]},
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    session.add(_step(run_id, 0, "sql", "pending"))
    session.add(_step(run_id, 1, "sql", "pending"))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "failed"
    assert steps[0].error is not None and "ns.bronze" in steps[0].error
    assert steps[1].status == "pending"
    assert (await _run_row(session, run_id)).status == "failed"


async def test_a_failed_query_stops_the_chain_and_fails_the_run(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Câu SQL chạy rồi HỎNG -> bước hỏng, bước sau KHÔNG chạy.

    Bước SQL thứ hai dựng gold từ silver; chạy nó trên một silver không dựng
    được là đúng loại hỏng không ai phát hiện — bảng ra đời, không lỗi nào báo,
    và con số thì sai.
    """
    _launcher(api_world)
    query = _query_service(api_world)
    query_id = str(uuid.uuid4())
    query.set_status(query_id, status="failed", error="Binder Error: no such table bronze.orders")

    lakehouse_id = await _lakehouse(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [_sql_step(lakehouse_id), _sql_step(lakehouse_id)]},
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    session.add(_step(run_id, 0, "sql", "running", query_id=query_id))
    session.add(_step(run_id, 1, "sql", "pending"))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "failed"
    assert steps[0].error == "Binder Error: no such table bronze.orders"
    assert steps[1].status == "pending"
    assert query.submitted == [], "chuỗi đã dừng, không được nộp câu SQL nào"
    run = await _run_row(session, run_id)
    assert run.status == "failed"
    assert run.finished_at is not None


async def test_a_query_loom_query_no_longer_knows_fails_the_step(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """`GET /query/{id}` trả 404 -> bước hỏng, không treo mãi.

    `loom-query` giữ trạng thái query TRONG BỘ NHỚ (xem `loom_query.store`), nên
    một lần restart pod làm mọi query đang chạy biến mất. Để bước nằm ở `running`
    trong trường hợp đó là một pipeline treo vĩnh viễn mà không ai được báo — và
    nó còn giữ chốt "không tự giẫm", nên pipeline đó ngừng chạy hẳn.
    """
    _launcher(api_world)
    query = _query_service(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [_sql_step(lakehouse_id)]},
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    # Một `query_id` mà `loom-query` không biết — chính là 404 của nó.
    session.add(_step(run_id, 0, "sql", "running", query_id=str(uuid.uuid4())))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "failed"
    assert steps[0].error is not None and "loom-query" in steps[0].error
    assert (await _run_row(session, run_id)).status == "failed"
    assert query.submitted == [], "không nộp lại — câu SQL có thể đã chạy rồi"


async def test_a_sql_step_still_running_leaves_the_run_running(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """`status: running` -> để nguyên. Một câu SQL quét vài phút là bình thường,
    và tick KHÔNG được đứng chờ nó (spec mục 2): nó hỏi lại ở nhịp sau."""
    _launcher(api_world)
    query = _query_service(api_world)
    query_id = str(uuid.uuid4())
    query.set_status(query_id, status="running")

    lakehouse_id = await _lakehouse(session, api_world)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {"schema_version": 1, "steps": [_sql_step(lakehouse_id), _sql_step(lakehouse_id)]},
    )

    run_id = await _running_run(session, api_world, pipeline_id)
    session.add(_step(run_id, 0, "sql", "running", query_id=query_id))
    session.add(_step(run_id, 1, "sql", "pending"))
    await session.commit()

    response = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert response.status_code == 200, response.text

    steps = await _steps_of(session, run_id)
    assert steps[0].status == "running"
    assert steps[1].status == "pending"
    assert query.submitted == []
    assert (await _run_row(session, run_id)).status == "running"


# ------------------------------------------------------- chuỗi đầy đủ, đầu-cuối


async def test_a_scheduled_ingest_then_sql_pipeline_reaches_succeeded(
    api_world: ApiWorld, session: AsyncSession
) -> None:
    """Nghiệm thu mục 4: một pipeline `nạp -> SQL` ĐƯỢC LẬP LỊCH chạy trọn.

    Ba nhịp, vì không có callback nào và tick POLL (xem docstring
    `routers/internal_schedule.py`):

    1. Lịch tới hạn -> `pipeline_run`, bước 0 chạy, Job lên.
    2. Pod báo xong (ở đây: hàng `ingest_run` thành `succeeded`, đúng thứ
       `/internal/ingest/{id}/complete` ghi) -> tick đóng bước 0 và NỘP bước 1.
    3. `loom-query` báo xong -> tick đóng bước 1, và run `succeeded`.

    Các nhịp sau nhịp đầu vẫn tới hạn theo cron mỗi-phút, và chúng ghi hàng
    `skipped` vì run trước còn chạy — đúng chốt "không tự giẫm". Đó là nhiễu có
    thật của một lịch dày, nên phép kiểm khẳng định trên CHÍNH run đầu tiên chứ
    không trên "run duy nhất".
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    launcher = _launcher(api_world)
    query = _query_service(api_world)

    lakehouse_id = await _lakehouse(session, api_world)
    connection_id = await _connection(session, api_world)
    anchor = _minute_floor() - timedelta(hours=2)
    pipeline_id = await _insert_item(
        session,
        api_world,
        ItemType.pipeline,
        {
            "schema_version": 1,
            "steps": [
                _ingest_step(lakehouse_id, connection_id),
                _sql_step(
                    lakehouse_id, "CREATE TABLE silver.orders AS SELECT * FROM bronze.orders"
                ),
            ],
            "schedule": _schedule(api_world.user_id),
        },
        updated_at=anchor,
    )

    # --- nhịp 1: lịch tới hạn, bước nạp chạy
    first = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["runs_started"] == 1

    run_id = (await _runs_of(session, pipeline_id))[0].id
    steps = await _steps_of(session, run_id)
    assert [s.status for s in steps] == ["running", "pending"]
    assert steps[0].ingest_run_id is not None
    assert launcher.launched == [steps[0].ingest_run_id]
    assert query.submitted == [], "bước SQL chưa tới lượt"

    # --- pod nạp báo xong, đúng thứ `/internal/ingest/{id}/complete` ghi
    ingest_id = steps[0].ingest_run_id
    session.expunge_all()
    ingest_row = (
        await session.execute(select(IngestRun).where(IngestRun.id == ingest_id))
    ).scalar_one()
    ingest_row.status = "succeeded"
    ingest_row.rows_written = 1200
    ingest_row.finished_at = datetime.now(UTC)
    await session.commit()

    # --- nhịp 2: bước nạp đóng lại, bước SQL được nộp
    second = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert second.status_code == 200, second.text

    steps = await _steps_of(session, run_id)
    assert [s.status for s in steps] == ["succeeded", "running"]
    assert len(query.submitted) == 1
    assert steps[1].query_id == query.submitted[0]
    assert (await _run_row(session, run_id)).status == "running"

    # --- `loom-query` báo xong
    query.set_status(query.submitted[0], status="succeeded", row_count=1200)

    # --- nhịp 3: bước SQL đóng lại, và RUN xong
    third = await api_world.client.post("/internal/schedule/tick", headers=TICK_HEADERS)
    assert third.status_code == 200, third.text

    steps = await _steps_of(session, run_id)
    assert [s.status for s in steps] == ["succeeded", "succeeded"]
    run = await _run_row(session, run_id)
    assert run.status == "succeeded"
    assert run.finished_at is not None
    assert run.error is None
    assert len(launcher.launched) == 1, "đúng một Job cho cả chuỗi"
    assert len(query.submitted) == 1, "đúng một câu SQL cho cả chuỗi"
