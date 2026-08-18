"""Đường ĐỌC của pipeline run — `GET /pipelines/{id}/runs`, `GET /pipeline-runs` và
`GET /pipeline-runs/{id}`.

Chạy qua HTTP THẬT (`api_world`, xem `conftest.py`) chứ không gọi thẳng handler:
điều mạnh nhất ở đây là một cổng quyền, và một cổng đúng ở tầng dưới vẫn có thể
bị bỏ qua ở tầng router — chỉ đường HTTP mới thấy. Cùng lý do
`test_ingest_api.py` ghi.

**Không double nào cả, và đó là điều đáng nói.** Ba đường này chỉ đọc Postgres:
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
4. Danh sách XUYÊN pipeline không trả hàng của workspace người gọi không thấy —
   canh bằng HAI workspace và quyền ở ĐÚNG MỘT, vì một phép kiểm một-workspace
   xanh y nguyên khi cổng quyền bị bỏ hẳn.
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
    Workspace,
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


# ------------------------------------------------- danh sách xuyên pipeline


async def test_the_cross_pipeline_list_hides_runs_in_a_workspace_the_caller_cannot_see(
    api_world: ApiWorld,
) -> None:
    """HAI workspace, quyền ở ĐÚNG MỘT — và đó là điều làm phép canh này thấy được.

    Nếu cả hai pipeline nằm cùng một workspace thì một bản không có cổng quyền nào
    cũng XANH, vì mọi hàng đều đáng thấy. Đúng lỗi mà `test_the_run_row_is_pending_…`
    của 3b đã mắc: cả hai item ở `ws_a`, nên phép canh mù với chính thứ nó gọi tên.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)

    mine = await _pipeline(api_world, workspace_id=api_world.ws_a)
    theirs = await _pipeline(api_world, workspace_id=api_world.ws_b)
    mine_run = await _run(api_world, mine, scheduled_for=_minute(-1), workspace_id=api_world.ws_a)
    theirs_run = await _run(
        api_world,
        theirs,
        scheduled_for=_minute(-2),
        workspace_id=api_world.ws_b,
        status="failed",
        error="host=source-db.internal table=payroll",
    )

    response = await api_world.client.get("/api/v1/pipeline-runs")

    assert response.status_code == 200, response.text
    ids = [row["run_id"] for row in response.json()["items"]]
    assert str(mine_run) in ids
    assert str(theirs_run) not in ids, (
        "run của workspace người gọi không có quyền đã lọt ra — kèm cả chuỗi error "
        "mang tên host nguồn"
    )
    # Vế thứ hai, cùng lý do `test_a_run_the_caller_cannot_see_is_not_found` ghi: một
    # bản lọc đúng danh sách `run_id` mà vẫn tô thêm chi tiết ở đâu đó trong thân phản
    # hồi vẫn rò rỉ đúng thứ cổng này canh.
    assert "source-db.internal" not in response.text


async def test_the_cross_pipeline_list_hides_runs_of_a_soft_deleted_workspace(
    api_world: ApiWorld,
) -> None:
    """Xoá MỀM workspace thì run của nó rời khỏi Hub.

    Đây là phép canh cho vế `Workspace.state == ACTIVE` của `visible_items_select` —
    vế mà vòng quét lịch của 3b đã quên, làm workspace đã xoá vẫn phóng Job mãi mãi.
    Bỏ vế đó khỏi biểu thức thì test này chuyển xanh→đỏ.

    Bất đối xứng CỐ Ý: `GET /pipelines/{id}/runs` vẫn trả hàng này (đường đó không lọc
    `state`, vì lịch sử chạy sống lâu hơn pipeline). Khẳng định ở dưới nói ra đúng điều
    đó — không phải đẳng thức giữa hai đường.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_a)
    run_id = await _run(api_world, pipeline_id, scheduled_for=_minute(-1))

    maker = async_sessionmaker(api_world.engine, expire_on_commit=False)
    async with maker() as session:
        workspace = await session.get(Workspace, api_world.ws_a)
        assert workspace is not None
        workspace.state = "deleted"
        await session.commit()

    hub = await api_world.client.get("/api/v1/pipeline-runs")
    assert hub.status_code == 200, hub.text
    assert [row["run_id"] for row in hub.json()["items"]] == []

    # Đường theo-id KHÔNG lọc `state`, nên nó vẫn trả về. Bất đối xứng này là thiết kế.
    by_id = await api_world.client.get(f"/api/v1/pipeline-runs/{run_id}")
    assert by_id.status_code == 200, by_id.text


async def test_a_cursor_from_one_filter_is_rejected_by_another(
    api_world: ApiWorld,
) -> None:
    """Cursor mang dấu vết BỘ LỌC, nên dán sang bộ lọc khác là 400.

    Không có dấu vết đó, một cursor lấy ở `status=failed` sẽ lật sang trang thứ hai
    của một tập KHÁC — người dùng nhận dữ liệu đúng định dạng và sai nội dung, không
    có gì báo lỗi.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_a)
    for offset in range(3):
        await _run(api_world, pipeline_id, scheduled_for=_minute(-offset - 1), status="failed")

    first = await api_world.client.get("/api/v1/pipeline-runs?status=failed&limit=2")
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor, "cần một trang thứ hai để có cursor mà dán"

    moved = await api_world.client.get(f"/api/v1/pipeline-runs?status=running&cursor={cursor}")
    assert moved.status_code == 400, moved.text


async def test_an_unknown_status_value_is_a_422_not_an_empty_page(
    api_world: ApiWorld,
) -> None:
    """`?status=xong` là 422 kèm danh sách giá trị hợp lệ, KHÔNG một trang rỗng.

    Một trang rỗng không phân biệt được với "không có run nào hỏng" — và người dùng
    gõ sai một chữ sẽ tin là hệ thống đang khoẻ.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)

    response = await api_world.client.get("/api/v1/pipeline-runs?status=xong")

    assert response.status_code == 422, response.text


async def test_a_run_appears_once_when_two_grants_both_match_it(
    api_world: ApiWorld,
) -> None:
    """HAI quyền cùng khớp một run, và nó vẫn ra ĐÚNG MỘT hàng.

    `visible_pipeline_runs_select` JOIN vào một subquery dựng từ `visible_items_select`,
    và bên trong đó điều kiện quyền là một `EXISTS` tương quan — nên nó chỉ lọc, không
    nhân hàng. Nếu ai đó đổi `EXISTS` thành một `JOIN role_assignment` (một cách viết
    trông tương đương), mỗi hàng quyền khớp sẽ thành một bản sao của cùng một run, và
    Hub đếm sai số lần chạy hỏng. Hai scope cùng khớp là cấu hình thật, không phải dựng
    ra: một admin cấp tenant được gán thêm ở một workspace cụ thể.

    Không dùng `.distinct()` để chữa nếu nó vỡ: nhân hàng ở đây là dấu hiệu biểu thức
    quyền đã đổi hình dạng, và `DISTINCT` chỉ che chỗ đó lại.

    Chỉ hai scope chứ không cả bốn: teardown của `api_world` dọn `role_assignment` theo
    `scope_id` thuộc `{ws_a, ws_b, tenant}`, nên một grant cấp ITEM sẽ sót lại và làm
    lệnh xoá `app_user` vỡ vì khoá ngoại. Hai vế đã đủ để phân biệt `EXISTS` với `JOIN`.
    """
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_a)
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    await api_world.grant(("tenant", DEFAULT_TENANT_ID), Role.admin)
    run_id = await _run(api_world, pipeline_id, scheduled_for=_minute(-1))

    response = await api_world.client.get("/api/v1/pipeline-runs")

    assert response.status_code == 200, response.text
    ids = [row["run_id"] for row in response.json()["items"]]
    assert ids == [str(run_id)], f"cùng một run ra nhiều lần: {ids}"


async def test_the_since_filter_reads_a_mark_without_an_offset_as_utc(
    api_world: ApiWorld,
) -> None:
    """`?since=` THIẾU offset phải cắt đúng như bản có `Z` — cả một VÒNG HTTP đầy đủ.

    Pydantic nhận một mốc thiếu offset thành `datetime` NAIVE, và một datetime naive
    đưa vào phép so với cột `timestamptz` bị asyncpg dịch theo giờ ĐỊA PHƯƠNG của tiến
    trình API. Kết quả là một tập hàng SAI, trả về 200 và không có gì báo lỗi.

    Việc canh HỢP ĐỒNG "không offset nghĩa là UTC" nằm ở `_since_utc`, và phép canh của
    nó là `test_pipeline_run_filters.py` — unit, đỏ trên MỌI máy. Phần phép canh NÀY
    thêm vào là thứ unit test không với tới: rằng chuỗi query → Pydantic → `_since_utc`
    → asyncpg → Postgres thực sự nối đúng, và rằng handler gọi `_since_utc` chứ không
    bỏ quên nó. Nếu ai xoá lời gọi đó, dòng dưới đây đỏ ở đúng máy có offset khác 0
    (máy đã viết nó là `Asia/Ho_Chi_Minh`, +07) — nên nó là phép canh về đường DÂY, còn
    phép canh về công thức thì không phụ thuộc múi giờ.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_a)
    # Cách nhau 90 phút, và mốc cắt nằm GIỮA — xa hơn mọi offset múi giờ thực tế thì
    # một phép dịch giờ sẽ không đổi kết quả, nên khoảng cách này cố ý nhỏ hơn thế.
    await _run(api_world, pipeline_id, scheduled_for=_minute(-90))
    recent = await _run(api_world, pipeline_id, scheduled_for=_minute(-1))
    cutoff = _minute(-30)

    naive = await api_world.client.get(
        "/api/v1/pipeline-runs", params={"since": cutoff.replace(tzinfo=None).isoformat()}
    )
    assert naive.status_code == 200, naive.text
    assert [row["run_id"] for row in naive.json()["items"]] == [str(recent)]

    # Cùng một mốc, viết tường minh — hai dạng phải cho CÙNG một tập.
    explicit = await api_world.client.get(
        "/api/v1/pipeline-runs", params={"since": cutoff.isoformat()}
    )
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["items"] == naive.json()["items"]


async def test_a_limit_below_one_is_a_422_not_a_500(api_world: ApiWorld) -> None:
    """`?limit=0` và `?limit=-1` là 422, và TỪNG là 500 ở cả hai đường danh sách.

    Cơ chế: `Page.build` nhận `limit <= 0`, thấy `len(rows) > limit`, cắt `rows[:limit]`
    thành rỗng rồi đọc `kept[-1]` → `IndexError` → 500. Cần ÍT NHẤT một hàng mới dựng
    lại được, nên phép canh dựng một run trước; không có nó thì `rows` rỗng, nhánh
    `has_more` không chạy, và một trang rỗng che mất lỗi.

    422 chứ không lặng lẽ nâng lên 1: `?limit=0` không có ý định nào đọc được, và trả
    một hàng cho người hỏi không hàng nào là trả lời một câu hỏi khác. TRẦN thì vẫn cắt
    im lặng (`_MAX_LIMIT`) — xem lý do ở `RunPageLimit`.

    Cả hai đường một lượt: sàn khai ở MỘT chỗ, nên một phép canh chỉ chạm đường mới sẽ
    xanh y nguyên khi ai đó chỉ gỡ nó khỏi một trong hai chữ ký.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_a)
    await _run(api_world, pipeline_id, scheduled_for=_minute(-1))

    for limit in (0, -1):
        hub = await api_world.client.get(f"/api/v1/pipeline-runs?limit={limit}")
        assert hub.status_code == 422, f"limit={limit} trên Hub: {hub.status_code} {hub.text}"
        by_pipeline = await api_world.client.get(
            f"/api/v1/pipelines/{pipeline_id}/runs?limit={limit}"
        )
        assert by_pipeline.status_code == 422, (
            f"limit={limit} trên đường một-pipeline: {by_pipeline.status_code} {by_pipeline.text}"
        )


async def test_a_limit_above_the_cap_still_answers_instead_of_refusing(
    api_world: ApiWorld,
) -> None:
    """`?limit=5000` vẫn là 200, KHÔNG thành 422.

    Đây là vế còn lại của bất đối xứng ở `RunPageLimit`: cách sửa sàn dễ nhất —
    `Query(ge=1, le=_MAX_LIMIT)` — sẽ lặng lẽ biến trần thành 422 và phá hợp đồng mà
    `GET /pipelines/{id}/runs` đã phát hành. Phép canh này khoá đúng điều đó lại.

    Nó KHÔNG canh phép cắt. Thế giới ở đây có một hàng, nên `min(limit, _MAX_LIMIT)` và
    `limit` cho cùng một phản hồi — đã đo: thay `capped = min(limit, _MAX_LIMIT)` bằng
    `capped = limit` ở cả hai handler thì hai phép canh về `limit` vẫn XANH. Nói ra chứ
    không để cái tên hàm hứa hộ.

    Và không test nào trong repo canh con số 200, trên đường này hay đường nào khác:
    muốn thấy phép cắt phải dựng 201 hàng, và đó là một phép canh đắt cho một tính chất
    rẻ. Ghi lại ở đây để lần sau ai đó đổi `_MAX_LIMIT` thì biết bộ test không thấy.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    pipeline_id = await _pipeline(api_world, workspace_id=api_world.ws_a)
    await _run(api_world, pipeline_id, scheduled_for=_minute(-1))

    hub = await api_world.client.get("/api/v1/pipeline-runs?limit=5000")
    assert hub.status_code == 200, hub.text
    # Một hàng, và trang không có `next_cursor`: một `limit` lớn phải TRẢ LỜI, không
    # phải từ chối. Số 1 ở đây là số hàng thế giới có, không phải một phép đo về trần.
    assert len(hub.json()["items"]) == 1
    assert hub.json()["next_cursor"] is None
    by_pipeline = await api_world.client.get(f"/api/v1/pipelines/{pipeline_id}/runs?limit=5000")
    assert by_pipeline.status_code == 200, by_pipeline.text
