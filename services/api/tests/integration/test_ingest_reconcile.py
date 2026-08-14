"""`GET /api/v1/ingest/{run_id}` — đọc một run, và ĐỐI CHIẾU nó với Job của nó.

Ở `integration/` chứ không ở `services/api/tests/`: mọi phép kiểm dưới đây bắt
đầu từ một HÀNG `ingest_run` đã commit và kết thúc bằng một câu hỏi về hàng đó
sau khi request chạy — `services/api/tests/conftest.py` không có database nào.
Cùng đính chính đã áp cho Task 9 và Task 10.

Chạy qua HTTP THẬT trên `api_world`: cổng quyền của đường này hỏi `item.read`
trên LAKEHOUSE của run, và một cổng quyền chỉ đúng khi nó nằm trên đường mà
người dùng thật đi qua — xem docstring `test_ingest_api.py`.

`JobLauncher` thật bị thay bằng một double gắn vào `app.state.job_launcher` SAU
khi app đã dựng, đúng khuôn `test_ingest_api.py`. Ở đây double còn ghi lại MỌI
lần `status()` được gọi, vì một trong những tính chất quan trọng nhất của file
này là một lời gọi KHÔNG xảy ra: không ai được hỏi Kubernetes về một run đã kết
thúc.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.jobs import JobStatus
from loom_api.models import DEFAULT_TENANT_ID, IngestRun, Item
from loom_core.item_definitions import ItemType
from loom_core.roles import Role

from .conftest import ApiWorld

pytestmark = pytest.mark.integration

STREAM = "public.orders"


class _FakeLauncher:
    """Double cho `JobLauncher` — hai phương thức của `JobLauncherLike`, không hơn.

    Không `MagicMock`: một mock trả về một `Mock` cho `status()` và nó có mọi
    thuộc tính, nên `job.exists` là một giá trị đúng-truthy và MỌI phép kiểm
    dưới đây xanh cho một bản cài đặt không đọc gì cả. `JobStatus` thật là thứ
    duy nhất buộc bốn trường phải được đọc đúng tên.

    `status()` ném `KeyError` cho một run mà bài test chưa khai trạng thái Job:
    im lặng trả về một mặc định sẽ biến một bài test thiếu tiền đề thành một bài
    test xanh về một tình huống không ai dựng.
    """

    def __init__(self) -> None:
        self.launched: list[uuid.UUID] = []
        self.status_calls: list[uuid.UUID] = []
        self._statuses: dict[uuid.UUID, JobStatus] = {}

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
        return self._statuses[run_id]


def _maker(world: ApiWorld) -> async_sessionmaker[Any]:
    return async_sessionmaker(world.engine, expire_on_commit=False)


def _launcher(world: ApiWorld) -> _FakeLauncher:
    launcher = _FakeLauncher()
    world.app.state.job_launcher = launcher
    return launcher


async def _lakehouse(world: ApiWorld, workspace_id: uuid.UUID) -> uuid.UUID:
    """Một hàng `item` kiểu `lakehouse` đã COMMIT, KHÔNG qua `ItemStore.create`.

    Cùng lý do `test_ingest_api.py` ghi: tạo một lakehouse qua store sẽ gọi
    `provision_warehouse`, tức là đòi một Lakekeeper thật — thứ file này không
    cần và không nên phụ thuộc vào.
    """
    item_id = uuid.uuid4()
    async with _maker(world)() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type=str(ItemType.lakehouse),
                name=f"lake-{item_id.hex[:8]}",
                display_name="lake",
                definition={"schema_version": 1},
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


async def _run(
    world: ApiWorld,
    *,
    status: str = "pending",
    workspace_id: uuid.UUID | None = None,
) -> IngestRun:
    """Một hàng `ingest_run` đã COMMIT ở trạng thái `status`.

    `connection_id` trỏ vào chính lakehouse: khoá ngoại của cột là `item.id`
    (xem `models.py`) nên hàng hợp lệ, và không phép kiểm nào ở file này đọc
    connection — đường `GET` chỉ tra lakehouse để hỏi quyền. Dựng thêm một item
    connection thật ở đây sẽ là một tiền đề không ai dùng.
    """
    workspace = workspace_id or world.ws_a
    lakehouse_id = await _lakehouse(world, workspace)
    run = IngestRun(
        id=uuid.uuid4(),
        lakehouse_id=lakehouse_id,
        connection_id=lakehouse_id,
        workspace_id=workspace,
        stream=STREAM,
        mode="incremental",
        status=status,
    )
    async with _maker(world)() as session:
        session.add(run)
        await session.commit()
    return run


async def _row(world: ApiWorld, run_id: uuid.UUID) -> IngestRun:
    async with _maker(world)() as session:
        return (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()


async def test_a_running_run_whose_job_vanished_becomes_failed(api_world: ApiWorld) -> None:
    """Không có bước này, một pod bị OOMKill để lại run `running` VĨNH VIỄN, và
    người dùng nhìn một thanh tiến trình không bao giờ dừng — `running` nghĩa là
    "pod đã lấy spec ít nhất một lần", KHÔNG phải "pod còn sống" (xem docstring
    `IngestRun.status`).

    `ttl_seconds_after_finished=3600` chỉ chạy SAU KHI Job kết thúc (xem
    `jobs.py`), nên một Job còn đang chạy không bao giờ bị TTL dọn: "Job không
    còn tồn tại" đọc được là "đã kết thúc từ lâu, hoặc chưa từng được tạo",
    không phải "chưa kịp khởi động".

    Khẳng định cả trên HÀNG Postgres, không chỉ trên thân phản hồi: một bản cài
    đặt tính ra `failed` rồi trả về mà không commit sẽ xanh ở vế thứ nhất trong
    khi cột `status` vẫn nói `running` cho mọi người đọc khác (một câu SQL tay,
    một lần điều tra sự cố), và lần đọc sau lại hỏi Kubernetes về cùng một run
    đã chết.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="running")
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=False))

    response = await api_world.client.get(f"/api/v1/ingest/{run.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "job" in body["error"].lower()
    row = await _row(api_world, run.id)
    assert row.status == "failed"
    assert row.error is not None
    assert row.finished_at is not None, (
        "một run đã đóng phải có `finished_at` — cột đó là thứ duy nhất nói run "
        "kết thúc lúc nào, và `/complete` (đường đóng run bình thường) đặt nó"
    )


async def test_a_pending_run_whose_job_never_started_becomes_failed(api_world: ApiWorld) -> None:
    """`pending` + Job có pod ĐÃ HỎNG (`failed=1`) — thường là sai tên Secret,
    nên pod kẹt ở `CreateContainerConfigError` rồi chết. Không có bước này, run
    nằm ở `pending` mãi (xem docstring `IngestRun.status`).

    `backoff_limit=0` (xem `jobs.py`) nghĩa là Kubernetes KHÔNG thử lại, nên một
    pod hỏng là hết đường — không có "lần thử thứ hai" nào để chờ.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="pending")
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=True, failed=1))

    body = (await api_world.client.get(f"/api/v1/ingest/{run.id}")).json()

    assert body["status"] == "failed"
    assert (await _row(api_world, run.id)).status == "failed"


async def test_a_pending_run_whose_job_is_still_starting_stays_pending(
    api_world: ApiWorld,
) -> None:
    """`pending` + Job `exists=True, active=1` → ĐỂ NGUYÊN `pending`.

    Đây là khoảng giữa "Job vừa được tạo" và "pod gọi `/spec` lần đầu", và MỌI
    lần nạp đều đi qua nó — đánh `failed` ở đây là giết mọi run trong vài giây
    đầu tiên của nó. Đó là lỗi tệ nhất mà đường đối chiếu này có thể mắc, vì nó
    hỏng ở đúng trường hợp thường gặp nhất và hỏng theo hướng người dùng không
    làm gì được.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="pending")
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=True, active=1))

    body = (await api_world.client.get(f"/api/v1/ingest/{run.id}")).json()

    assert body["status"] == "pending"
    assert body["error"] is None
    assert (await _row(api_world, run.id)).status == "pending"


async def test_a_running_run_whose_pod_is_alive_stays_running(api_world: ApiWorld) -> None:
    """`running` + Job `active=1` → ĐỂ NGUYÊN `running`. Pod đang chạy, và một
    lần nạp dài (giờ, không phút) là chuyện bình thường."""
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="running")
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=True, active=1))

    body = (await api_world.client.get(f"/api/v1/ingest/{run.id}")).json()

    assert body["status"] == "running"
    assert body["error"] is None
    assert (await _row(api_world, run.id)).status == "running"


async def test_a_job_that_succeeded_without_reporting_is_failed_not_succeeded(
    api_world: ApiWorld,
) -> None:
    """Job `succeeded=1` nhưng hàng vẫn `running` → `failed`, KHÔNG `succeeded`.

    Đây là hình dạng của một pod bị OOMKill ngay sau lô cuối: Kubernetes thấy
    một container thoát 0 ở lần thử duy nhất, nhưng `/complete` chưa bao giờ
    tới. Suy ra `succeeded` là nói dối bằng một con số ta không có:
    `rows_written` chỉ tiến qua `/progress`, và `/complete` là nguồn sự thật DUY
    NHẤT cho việc run đã kết thúc đúng (xem `routers/internal_ingest.py`).
    Người đọc phải biết rằng bảng đích có thể thiếu dữ liệu.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="running")
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=True, succeeded=1))

    body = (await api_world.client.get(f"/api/v1/ingest/{run.id}")).json()

    assert body["status"] == "failed"
    assert "job" in body["error"].lower()
    assert (await _row(api_world, run.id)).status == "failed"


async def test_a_succeeded_run_is_never_re_examined(api_world: ApiWorld) -> None:
    """Trạng thái cuối là CUỐI. Hỏi k8s về một run đã xong là vô nghĩa (Job đã
    bị TTL dọn sau một giờ) và sẽ biến `succeeded` thành `failed` sau một giờ —
    phép canh dễ bị bỏ nhất, và là phép canh bắt lỗi tệ nhất.

    `status_calls == []` là vế chính: chỉ khẳng định `status == "succeeded"` sẽ
    xanh y nguyên cho một bản cài đặt hỏi Kubernetes rồi tình cờ không đổi gì,
    tức là canh kết quả thay vì canh điều kiện.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="succeeded")
    launcher = _launcher(api_world)
    # Trạng thái mà một Job đã bị TTL dọn trả về — nếu vòng đối chiếu chạy, nó
    # sẽ đánh run này `failed`.
    launcher.set_status(run.id, JobStatus(exists=False))

    body = (await api_world.client.get(f"/api/v1/ingest/{run.id}")).json()

    assert body["status"] == "succeeded"
    assert launcher.status_calls == []


async def test_a_failed_run_is_never_re_examined(api_world: ApiWorld) -> None:
    """`failed` cũng là trạng thái CUỐI, và nó có lý do riêng để không hỏi lại:
    một run đã `failed` mang `error` do pod tự báo (xem `/complete`), thứ nói
    đúng nguyên nhân; một lượt đối chiếu nữa sẽ ghi đè nó bằng câu chung chung
    "Job không còn tồn tại" và người vận hành mất đúng dòng cần đọc."""
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="failed")
    async with _maker(api_world)() as session:
        row = (await session.execute(select(IngestRun).where(IngestRun.id == run.id))).scalar_one()
        row.error = "SourceUnreachable: could not connect to db.example.internal"
        await session.commit()
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=False))

    body = (await api_world.client.get(f"/api/v1/ingest/{run.id}")).json()

    assert body["status"] == "failed"
    assert body["error"] == "SourceUnreachable: could not connect to db.example.internal"
    assert launcher.status_calls == []


async def test_a_viewer_can_read_a_run(api_world: ApiWorld) -> None:
    """Đọc trạng thái nạp là ĐỌC, nên `viewer` là đủ — đòi `contributor` ở đây
    sẽ khoá mất trường hợp bình thường (một người được chia sẻ ở mức xem muốn
    biết dữ liệu đã tới chưa), trong khi KHỞI ĐỘNG một lần nạp thì vẫn đòi
    `contributor` (xem `test_ingest_api.py`)."""
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    run = await _run(api_world, status="running")
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=True, active=1))

    response = await api_world.client.get(f"/api/v1/ingest/{run.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(run.id)
    assert body["lakehouse_id"] == str(run.lakehouse_id)
    assert (body["stream"], body["mode"]) == (STREAM, "incremental")
    assert body["rows_written"] == 0


async def test_a_run_of_a_lakehouse_the_caller_cannot_see_is_not_found(
    api_world: ApiWorld,
) -> None:
    """404, và KHÔNG một lời gọi Kubernetes nào.

    `lakehouse_id` lấy từ HÀNG `ingest_run`, không bao giờ từ client. Không có
    cổng này, bất kỳ ai đã đăng nhập cũng đọc được trạng thái nạp của MỌI
    workspace — kèm `error`, thứ thường mang tên host nguồn và tên bảng.

    404 chứ không 403: một run của lakehouse người gọi không thấy phải không
    phân biệt được với một run không tồn tại, cùng lý do `NotVisible` tồn tại
    (xem `permissions.py`).

    Lakehouse ở `ws_b` và người gọi có `contributor` trên `ws_a` — CỐ Ý có một
    quyền ở đâu đó: với một principal không quyền gì cả, câu khẳng định 404 vẫn
    xanh cho một bản cài đặt hỏi quyền trên SAI tài nguyên (hoặc không hỏi gì
    mà chỉ tình cờ hỏng ở chỗ khác).

    `status_calls == []` là vế thứ hai: một bản cài đặt đối chiếu TRƯỚC rồi mới
    hỏi quyền vẫn trả 404 cho người gọi, trong khi nó vừa đánh `failed` một run
    của workspace khác dựa trên một câu hỏi mà người này không được phép đặt.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    run = await _run(api_world, status="running", workspace_id=api_world.ws_b)
    launcher = _launcher(api_world)
    launcher.set_status(run.id, JobStatus(exists=False))

    response = await api_world.client.get(f"/api/v1/ingest/{run.id}")

    assert response.status_code == 404, response.text
    assert launcher.status_calls == []
    assert (await _row(api_world, run.id)).status == "running"


async def test_an_unknown_run_id_is_not_found(api_world: ApiWorld) -> None:
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    launcher = _launcher(api_world)

    response = await api_world.client.get(f"/api/v1/ingest/{uuid.uuid4()}")

    assert response.status_code == 404, response.text
    assert launcher.status_calls == []
