"""Đường ĐỌC của pipeline run — `GET /pipelines/{id}/runs` và `GET /pipeline-runs/{id}`.

Chạy qua HTTP THẬT (`api_world`, xem `conftest.py`) chứ không gọi thẳng handler:
điều mạnh nhất ở đây là một cổng quyền, và một cổng đúng ở tầng dưới vẫn có thể
bị bỏ qua ở tầng router — chỉ đường HTTP mới thấy. Cùng lý do
`test_ingest_api.py` ghi.

**Không double nào cả, và đó là điều đáng nói.** Hai đường này chỉ đọc Postgres:
không Kubernetes, không `loom-query`, không đối chiếu. Nên mọi phép kiểm dưới
đây dựng hàng thật rồi khẳng định trên phản hồi thật. Nếu một ngày file này phải
mọc ra một `_FakeLauncher`, đó là dấu hiệu đường đọc đã thôi chỉ đọc — xem
docstring `get_pipeline_run` về việc vì sao nó KHÔNG đối chiếu.

Hình dạng được canh, theo thứ tự quan trọng:

1. Một run người gọi không thấy được trả 404, và KHÔNG PHÂN BIỆT được với một
   run không tồn tại. Chứng minh đỏ bằng cách bỏ dòng `require_item` ở
   `get_pipeline_run`: `test_a_run_the_caller_cannot_see_is_not_found` chuyển
   404 → 200 kèm nguyên chuỗi `error` của bước.
2. Quyền hỏi trên ITEM PIPELINE lấy từ HÀNG, không từ client — canh bằng một
   pipeline ở workspace B mà người gọi chỉ có quyền ở workspace A.
3. Chuỗi bước ra đúng thứ tự `step_index` kèm lỗi của bước hỏng.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import (
    DEFAULT_TENANT_ID,
    Item,
    PipelineRun,
    PipelineStepRun,
)
from loom_core.item_definitions import ItemType
from loom_core.roles import Role

from .conftest import ApiWorld

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_pipeline_rows(api_world: ApiWorld) -> AsyncIterator[None]:
    """Dọn `pipeline_run`/`pipeline_step_run` SAU mỗi phép kiểm, TRƯỚC `api_world`.

    `pipeline_run.pipeline_id` có khoá ngoại tới `item.id`, nên teardown của
    `api_world` (xoá item) VỠ nếu còn sót hàng ở đây. Fixture này phụ thuộc
    `api_world` nên nó chạy trước. Cùng khuôn và cùng lý do với fixture cùng tên
    ở `test_internal_schedule.py`.
    """
    yield
    maker = async_sessionmaker(api_world.engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(delete(PipelineStepRun))
        await session.execute(delete(PipelineRun))
        await session.commit()


async def _insert_item(
    world: ApiWorld,
    item_type: ItemType,
    *,
    workspace_id: uuid.UUID | None = None,
    definition: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Một hàng `item` đã COMMIT, KHÔNG qua `ItemStore.create`.

    Đi qua store sẽ gọi `provision_warehouse` cho một lakehouse — tức đòi một
    Lakekeeper thật, thứ file này không cần. Cùng lý do `test_ingest_api.py` ghi.
    """
    item_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id or world.ws_a,
                type=str(item_type),
                name=f"{item_type}-{item_id.hex[:8]}",
                display_name=str(item_type),
                definition=definition or {"schema_version": 1, "steps": []},
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


async def _pipeline(world: ApiWorld, *, workspace_id: uuid.UUID | None = None) -> uuid.UUID:
    return await _insert_item(world, ItemType.pipeline, workspace_id=workspace_id)


async def _run(
    world: ApiWorld,
    pipeline_id: uuid.UUID,
    *,
    scheduled_for: datetime,
    status: str = "succeeded",
    workspace_id: uuid.UUID | None = None,
    error: str | None = None,
    skip_reason: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    """Một `pipeline_run` đã COMMIT, kèm các bước mô tả bằng dict."""
    run_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            PipelineRun(
                id=run_id,
                pipeline_id=pipeline_id,
                workspace_id=workspace_id or world.ws_a,
                scheduled_for=scheduled_for,
                status=status,
                skip_reason=skip_reason,
                error=error,
                run_as_user_id=world.user_id,
                started_at=scheduled_for + timedelta(seconds=4),
                finished_at=None if status in ("pending", "running") else scheduled_for,
            )
        )
        for step in steps or []:
            session.add(
                PipelineStepRun(
                    id=uuid.uuid4(),
                    pipeline_run_id=run_id,
                    **step,
                )
            )
        await session.commit()
    return run_id


def _minute(offset: int = 0) -> datetime:
    """Một mốc nhịp TÍNH ĐƯỢC, cắt về đầu phút — cùng khuôn `_minute_floor`
    ở `test_internal_schedule.py`."""
    return datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=offset)


# ----------------------------------------------------------------- chi tiết


async def test_a_run_comes_back_with_its_steps_in_index_order(api_world: ApiWorld) -> None:
    """Chuỗi bước ra theo `step_index` TĂNG DẦN, kể cả khi chèn ngược.

    Chèn ngược có chủ đích: không có `ORDER BY` thì Postgres trả về theo thứ tự
    vật lý, và với hai hàng vừa chèn thì thứ tự đó thường TRÙNG thứ tự chèn —
    nên một phép kiểm chèn xuôi sẽ xanh y nguyên khi `order_by` bị xoá. Thứ tự
    này không phải chuyện thẩm mỹ: nó là thứ tự CHẠY của một chuỗi tuyến tính,
    và một giao diện vẽ ngược nó nói sai bước nào chặn bước nào.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world)
    run_id = await _run(
        api_world,
        pipeline_id,
        scheduled_for=_minute(),
        status="failed",
        error="the SQL failed",
        steps=[
            {
                "step_index": 1,
                "step_type": "sql",
                "status": "failed",
                "query_id": "q-123",
                "error": "the SQL failed",
            },
            {
                "step_index": 0,
                "step_type": "ingest",
                "status": "succeeded",
                "ingest_run_id": None,
            },
        ],
    )

    response = await api_world.client.get(f"/api/v1/pipeline-runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["pipeline_id"] == str(pipeline_id)
    assert body["status"] == "failed"
    assert body["run_as_user_id"] == str(api_world.user_id)
    assert [step["step_index"] for step in body["steps"]] == [0, 1]
    assert [step["step_type"] for step in body["steps"]] == ["ingest", "sql"]
    assert body["steps"][1]["query_id"] == "q-123"
    # Lỗi của bước phải đi RA. Đây là trường mang nhiều thông tin nhất trong cả
    # phản hồi, và là lý do cổng quyền ở trên tồn tại.
    assert body["steps"][1]["error"] == "the SQL failed"


async def test_a_run_the_caller_cannot_see_is_not_found(api_world: ApiWorld) -> None:
    """404 cho một run trong workspace người gọi không có quyền gì.

    **Đây là phép kiểm chứng minh đỏ được của Task 12.** Bỏ dòng
    `require_item(run.pipeline_id, Action.item_read)` ở `get_pipeline_run` thì
    nó chuyển 404 → 200, và thân phản hồi mang nguyên chuỗi `error` của bước —
    thứ chép nguyên văn từ `ingest_run.error`, nên nó thường chứa tên host nguồn
    và tên bảng.

    404 chứ không 403: 403 xác nhận rằng CÓ một run với id này, tức là xác nhận
    sự tồn tại của một pipeline trong workspace người ta không được vào. Xem
    `NotVisible` ở `permissions.py`.
    """
    # Quyền ở ws_a; pipeline nằm ở ws_b. Không phải "không có quyền gì cả" —
    # hình dạng thật là một người dùng bình thường có quyền ở CHỖ KHÁC.
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_b)
    run_id = await _run(
        api_world,
        pipeline_id,
        scheduled_for=_minute(),
        status="failed",
        workspace_id=api_world.ws_b,
        error="could not connect to db.secret-host.internal: password authentication failed",
        steps=[
            {
                "step_index": 0,
                "step_type": "ingest",
                "status": "failed",
                "error": "could not connect to db.secret-host.internal",
            }
        ],
    )

    response = await api_world.client.get(f"/api/v1/pipeline-runs/{run_id}")

    assert response.status_code == 404, response.text
    # Vế thứ hai, và nó không thừa: một bản cài đặt trả 404 kèm thân lỗi có tô
    # thêm chi tiết run vẫn rò rỉ đúng thứ cổng này canh.
    assert "secret-host" not in response.text


async def test_a_run_that_does_not_exist_answers_exactly_like_an_invisible_one(
    api_world: ApiWorld,
) -> None:
    """Hai nhánh 404 phải KHÔNG phân biệt được ở mã trạng thái.

    Nếu chúng khác nhau thì cổng ở trên vô nghĩa: người gọi vẫn dò được id nào
    có thật bằng cách so hai câu trả lời.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)

    response = await api_world.client.get(f"/api/v1/pipeline-runs/{uuid.uuid4()}")

    assert response.status_code == 404, response.text


async def test_a_viewer_can_read_a_run(api_world: ApiWorld) -> None:
    """`item.read`, KHÔNG `item.update`: một người được chia sẻ ở mức xem phải
    biết được đêm qua pipeline có chạy không.

    Đỏ nếu ai đó nâng cổng lên `Action.item_update` — một thay đổi trông vô hại
    ("run là chuyện vận hành") nhưng khoá mất đúng trường hợp thường gặp nhất.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world)
    run_id = await _run(api_world, pipeline_id, scheduled_for=_minute())

    response = await api_world.client.get(f"/api/v1/pipeline-runs/{run_id}")

    assert response.status_code == 200, response.text
    assert response.json()["steps"] == []


async def test_a_skipped_run_says_why_and_carries_no_error(api_world: ApiWorld) -> None:
    """`skip_reason` và `error` là HAI trường, và một nhịp bị bỏ không phải lỗi.

    Gộp chúng lại (hay đổ `skip_reason` vào `error`) sẽ làm Monitor Hub tô đỏ
    một thứ đúng theo thiết kế — xem `schedule_service.decide`.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world)
    run_id = await _run(
        api_world,
        pipeline_id,
        scheduled_for=_minute(),
        status="skipped",
        skip_reason="a previous run is still running",
    )

    response = await api_world.client.get(f"/api/v1/pipeline-runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "skipped"
    assert body["skip_reason"] == "a previous run is still running"
    assert body["error"] is None


# ------------------------------------------------------------------ danh sách


async def test_the_list_is_newest_first_and_pages_without_repeating(api_world: ApiWorld) -> None:
    """Ba nhịp, hai trang, không hàng nào lặp hay biến mất.

    Sắp theo `scheduled_for` GIẢM DẦN — mốc NHỊP, không phải `started_at`. Phép
    kiểm dựng ba nhịp CÁCH NHAU về `scheduled_for` nhưng `started_at` suy ra từ
    chính chúng, nên một bản cài đặt sắp theo `started_at` vẫn xanh ở đây; điều
    canh được là thứ tự giảm dần và tính đúng của cursor, thứ đã hỏng thật
    trong repo này khi khoá keyset chỉ có một cột (xem `pagination.py`).
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world)
    # Chèn theo thứ tự nhịp TĂNG DẦN, khẳng định phản hồi GIẢM DẦN: chèn xuôi
    # rồi mong ngược nghĩa là thứ tự vật lý của Postgres không thể tình cờ đúng.
    oldest = await _run(api_world, pipeline_id, scheduled_for=_minute(-3))
    middle = await _run(api_world, pipeline_id, scheduled_for=_minute(-2))
    newest = await _run(api_world, pipeline_id, scheduled_for=_minute(-1))

    first = await api_world.client.get(f"/api/v1/pipelines/{pipeline_id}/runs?limit=2")
    assert first.status_code == 200, first.text
    page_one = first.json()
    assert [row["run_id"] for row in page_one["items"]] == [str(newest), str(middle)]
    assert page_one["next_cursor"] is not None
    # Bản tóm tắt KHÔNG mang bước — xem docstring `PipelineRunSummary`.
    assert "steps" not in page_one["items"][0]

    second = await api_world.client.get(
        f"/api/v1/pipelines/{pipeline_id}/runs?limit=2&cursor={page_one['next_cursor']}"
    )
    assert second.status_code == 200, second.text
    page_two = second.json()
    assert [row["run_id"] for row in page_two["items"]] == [str(oldest)]
    assert page_two["next_cursor"] is None


async def test_the_list_only_shows_runs_of_the_pipeline_asked_for(api_world: ApiWorld) -> None:
    """Hai pipeline trong CÙNG workspace không được trộn run của nhau.

    Không có phép kiểm này, một `WHERE` bị bỏ quên vẫn xanh ở mọi phép kiểm
    khác trong file — chúng đều chỉ có một pipeline.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    mine = await _pipeline(api_world)
    other = await _pipeline(api_world)
    my_run = await _run(api_world, mine, scheduled_for=_minute(-1))
    await _run(api_world, other, scheduled_for=_minute(-1))

    response = await api_world.client.get(f"/api/v1/pipelines/{mine}/runs")

    assert response.status_code == 200, response.text
    assert [row["run_id"] for row in response.json()["items"]] == [str(my_run)]


async def test_a_pipeline_the_caller_cannot_see_is_not_found(api_world: ApiWorld) -> None:
    """404 cho danh sách run của một pipeline người gọi không thấy — không phải
    một danh sách rỗng. Rỗng là một câu trả lời KHÁC ("pipeline này chưa chạy
    lần nào"), và nói nó ở đây là xác nhận id đó có thật."""
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_b)
    await _run(api_world, pipeline_id, scheduled_for=_minute(), workspace_id=api_world.ws_b)

    response = await api_world.client.get(f"/api/v1/pipelines/{pipeline_id}/runs")

    assert response.status_code == 404, response.text


async def test_an_id_that_is_not_a_pipeline_is_not_found(api_world: ApiWorld) -> None:
    """Một id lakehouse trên đường dẫn này ra 404, KHÔNG ra một trang rỗng.

    Người gọi THẤY ĐƯỢC item đó (quyền cấp workspace), nên đây không phải chuyện
    rò rỉ — nó là chuyện chẩn đoán: một trang rỗng không phân biệt được với
    "pipeline này chưa từng chạy", và một UI gửi nhầm id sẽ hiện "không có run
    nào" mãi mãi thay vì báo lỗi.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    lakehouse_id = await _insert_item(
        api_world, ItemType.lakehouse, definition={"schema_version": 1}
    )

    response = await api_world.client.get(f"/api/v1/pipelines/{lakehouse_id}/runs")

    assert response.status_code == 404, response.text


async def test_a_cursor_from_another_pipeline_is_rejected(api_world: ApiWorld) -> None:
    """Cursor mang dấu vết bộ lọc, nên dán nó sang pipeline khác là 400.

    Không có dấu vết đó, cursor của pipeline A áp lên pipeline B sẽ trả về một
    trang dữ liệu trông hợp lệ nhưng cắt ở một mốc không liên quan — im lặng và
    sai. Xem `_filter_fingerprint` ở `pagination.py`.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    mine = await _pipeline(api_world)
    other = await _pipeline(api_world)
    for offset in (-2, -1):
        await _run(api_world, mine, scheduled_for=_minute(offset))
    await _run(api_world, other, scheduled_for=_minute(-1))

    first = await api_world.client.get(f"/api/v1/pipelines/{mine}/runs?limit=1")
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    response = await api_world.client.get(f"/api/v1/pipelines/{other}/runs?cursor={cursor}")

    assert response.status_code == 400, response.text
