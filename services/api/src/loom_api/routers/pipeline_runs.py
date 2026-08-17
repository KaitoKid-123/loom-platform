"""HAI đường ĐỌC cho các lần chạy được lập lịch của một pipeline:

- `GET /api/v1/pipelines/{pipeline_id}/runs` — danh sách run của MỘT pipeline,
  mới nhất trước, phân trang bằng cursor.
- `GET /api/v1/pipeline-runs/{run_id}` — MỘT run kèm cả chuỗi bước của nó.

Cả hai chỉ ĐỌC. Không có đường nào ở đây tạo, huỷ hay chạy lại một run: nguồn
duy nhất sinh ra `pipeline_run` là nhịp lịch (`routers/internal_schedule.py`),
và mở một đường thứ hai là mở một chỗ thứ hai để quyết định "nhịp này có được
chạy không" — đúng câu hỏi mà `schedule_service.decide` là nơi duy nhất trả lời.

## Vì sao hai đường này tồn tại

Trước chúng, cách DUY NHẤT để biết một pipeline đã chạy ra sao là hỏi thẳng
Postgres từ trong pod `loom-api`. Điều đó làm hai thứ không làm được: `make
smoke` chỉ nói HTTP (nó cố ý không dùng `kubectl` — xem đầu `scripts/smoke.sh`),
nên không có phép kiểm chấp nhận nào che được đường lập lịch; và Monitor Hub của
Giai đoạn 3c không có gì để vẽ.

Hình dạng vì thế nhắm vào cả hai người dùng đó: một DANH SÁCH để quét trạng thái
(một dòng một run, không kèm bước — xem `PipelineRunSummary`), và một CHI TIẾT
để mở đúng một run ra xem nó chết ở bước nào.

## Cổng quyền: `item.read` trên chính ITEM PIPELINE, và không gì khác

Một `pipeline_run` thuộc về một item kiểu `pipeline` — cột `pipeline_id` là khoá
ngoại tới `item.id`, và nó là thứ DUY NHẤT trong hàng chỉ tới một tài nguyên có
quyền. Các bước thì chạm lakehouse, nhưng `pipeline_step_run` KHÔNG mang
`lakehouse_id` nào: id đó nằm trong `definition` của pipeline
(`IngestStepConfig.lakehouse_id` / `SqlStepConfig.lakehouse_id`), không nằm
trong hàng.

Đã cân nhắc cổng "đọc được MỌI lakehouse mà chuỗi bước chạm tới" và bỏ, vì hai
lý do:

1. **Nó không ổn định.** Tập lakehouse nằm trong definition, và definition sửa
   được bất cứ lúc nào. Cùng một run — một sự kiện đã xảy ra và đóng lại — sẽ
   lúc thấy được lúc không, tuỳ một người khác vừa sửa gì trong pipeline sáng
   nay. Một quy tắc hiển thị mà câu trả lời đổi theo dữ liệu KHÔNG liên quan tới
   hàng đang đọc là quy tắc không ai suy luận được.
2. **Nó bắt đường đọc parse definition.** Tức là mở JSONB, dựng
   `PipelineDefinition`, và quyết định phải làm gì khi definition hỏng — trong
   một handler chỉ có việc trả lại một hàng. Một definition không parse được sẽ
   biến một run đã đóng thành 500.

Quy tắc thật sự đang theo là quy tắc của `get_ingest_run` (Giai đoạn 3a): **lấy
id tài nguyên TỪ HÀNG ĐÃ LƯU, không bao giờ từ client, rồi hỏi
`PermissionService.require_item`.** Ở đó cột đó là `ingest_run.lakehouse_id`; ở
đây là `pipeline_run.pipeline_id`. Cùng một luật, cùng một `PermissionService` —
không có bộ kiểm quyền thứ hai nào trong file này.

`item.read` chứ không `item.update`: xem trạng thái là ĐỌC, và đòi `contributor`
sẽ khoá mất trường hợp bình thường nhất (một người được chia sẻ ở mức xem muốn
biết đêm qua pipeline có chạy không).

**404, KHÔNG 403, cho một run người gọi không thấy** — `NotVisible` ở
`permissions.py`. Phân biệt "không có run nào" với "có nhưng không được thấy" là
xác nhận sự tồn tại của một run trong workspace người ta không được vào, và cột
`error` ở đây thường mang tên host nguồn cùng tên bảng (nó được chép NGUYÊN VĂN
từ `ingest_run.error` — xem `_reconcile_ingest_step`).

**Phần lộ ra CÒN LẠI, nói thẳng chứ không ngụ ý là đã hết:** ai đọc được item
pipeline thì đọc được `error` của các bước, và với bước nạp thì chuỗi đó có thể
mang HOST của connection — trong khi `definition` của chính pipeline chỉ có
`connection_id`, không có host. Với quyền cấp workspace trở lên thì không có
chênh lệch nào (người đó thấy luôn cả item connection); chênh lệch chỉ tồn tại
đúng ở một trường hợp hẹp: một grant cấp ITEM chỉ trên pipeline. Cái giá của
việc bịt nó là quy tắc không ổn định ở mục (1) trên, và một run báo hỏng mà
không nói được vì sao thì không đáng gọi là đường đọc.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.models import Item, PipelineRun, PipelineStepRun
from loom_api.pagination import Page, decode_cursor, encode_cursor
from loom_api.permissions import PermissionService
from loom_core.item_definitions import ItemType
from loom_core.roles import Action
from loom_core.schemas import (
    PageOut,
    PipelineRunDetail,
    PipelineRunSummary,
    PipelineStepRunOut,
    Principal,
)

router = APIRouter(tags=["pipeline-runs"])

_MAX_LIMIT = 200


def _summary(run: PipelineRun) -> PipelineRunSummary:
    return PipelineRunSummary(
        run_id=run.id,
        pipeline_id=run.pipeline_id,
        scheduled_for=run.scheduled_for,
        status=run.status,
        skip_reason=run.skip_reason,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _step_out(step: PipelineStepRun) -> PipelineStepRunOut:
    return PipelineStepRunOut(
        step_index=step.step_index,
        step_type=step.step_type,
        status=step.status,
        ingest_run_id=step.ingest_run_id,
        query_id=step.query_id,
        started_at=step.started_at,
        finished_at=step.finished_at,
        error=step.error,
    )


@router.get("/pipelines/{pipeline_id}/runs", response_model=PageOut)
async def list_pipeline_runs(
    pipeline_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    """Các run của một pipeline, MỚI NHẤT TRƯỚC.

    **Tra hàng item TRƯỚC, hỏi quyền SAU, và cả hai nhánh hỏng đều là 404** —
    cùng thứ tự và cùng lý do mà `start_ingest` ghi: hai nhánh cùng trả 404 nên
    thứ tự này không rò rỉ gì, và nó tiết kiệm một lượt hỏi quyền cho một id vô
    nghĩa.

    Lượt tra đó lọc theo `type`, KHÔNG theo `state`. Lọc `type` để một id
    lakehouse gõ nhầm vào đường dẫn này ra 404 chứ không ra một danh sách RỖNG —
    một trang trắng không phân biệt được với "pipeline này chưa từng chạy", và
    đó đúng là kiểu xanh-mà-không-nói-gì mà repo này cấm. KHÔNG lọc `state` vì
    lịch sử chạy sống LÂU HƠN pipeline: người vận hành mở màn hình lên lúc 9 giờ
    sáng để hỏi "đêm qua nó chạy ra sao" chính là lúc ai đó vừa xoá mềm nó, và
    một 404 ở đó là mất luôn thứ duy nhất còn lại nói được chuyện gì đã xảy ra.
    Đường chi tiết bên dưới cũng không lọc `state`, cùng lý do — và cả hai vì
    vậy trả lời như nhau cho cùng một pipeline.

    Sắp theo `(scheduled_for, id)` GIẢM DẦN, và cursor cũng trên đúng cặp đó.
    `scheduled_for` chứ không `started_at`: nó là MỐC NHỊP, thứ tự người ta nghĩ
    về các lần chạy ("nhịp 2 giờ sáng"), và nó ổn định — `started_at` là lúc
    tick nhìn thấy nhịp, nên một tick chậm làm hai run đảo chỗ nhau trong danh
    sách. Cặp chứ không một cột: `UNIQUE (pipeline_id, scheduled_for)` làm
    `scheduled_for` duy nhất TRONG một pipeline, nhưng khoá keyset một cột vẫn
    là một khoá không có thứ tự tổng ở tầng SQL, và `id` là vế phá hoà rẻ nhất —
    cùng khuôn `ItemStore.list_items` (xem `pagination.py` về việc vì sao khoá
    một cột làm lật trang nhảy hoặc lặp).
    """
    perms = PermissionService(session, principal)

    pipeline = (
        await session.execute(
            select(Item).where(Item.id == pipeline_id, Item.type == str(ItemType.pipeline))
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pipeline with this id")
    await perms.require_item(pipeline_id, Action.item_read)

    # Bộ lọc đi vào dấu vết cursor: một cursor của pipeline A dán sang pipeline B
    # phải là 400 chứ không phải một trang dữ liệu của người khác.
    filters = {"pipeline_id": str(pipeline_id)}

    stmt = select(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id)
    if cursor:
        after_ts, after_id = decode_cursor(cursor, filters)
        stmt = stmt.where(
            (PipelineRun.scheduled_for < after_ts)
            | ((PipelineRun.scheduled_for == after_ts) & (PipelineRun.id < after_id))
        )

    capped = min(limit, _MAX_LIMIT)
    # limit+1 để biết còn trang sau mà không cần COUNT — xem `Page.build`.
    stmt = stmt.order_by(PipelineRun.scheduled_for.desc(), PipelineRun.id.desc()).limit(capped + 1)
    rows = (await session.execute(stmt)).scalars().all()
    page = Page.build(
        rows, capped, cursor_of=lambda run: encode_cursor(run.scheduled_for, run.id, filters)
    )
    return PageOut(items=[_summary(run) for run in page.items], next_cursor=page.next_cursor)


@router.get("/pipeline-runs/{run_id}", response_model=PipelineRunDetail)
async def get_pipeline_run(
    run_id: uuid.UUID,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PipelineRunDetail:
    """Một run kèm chuỗi bước của nó — trả lại ĐÚNG những gì đã lưu.

    **KHÔNG đối chiếu gì cả, và đó là khác biệt lớn nhất so với
    `get_ingest_run`.** Đường 3a phải hỏi Kubernetes khi có người đọc vì ở 3a
    KHÔNG có vòng lặp nền nào — một pod bị OOMKill sau khi lấy spec nằm ở
    `running` vĩnh viễn nếu không ai đối chiếu. Ở 3b thì có: nhịp lịch chạy mỗi
    `scheduler.tickSeconds` giây, và `_advance_all_running_runs` đối chiếu MỌI
    run đang chạy ở mỗi nhịp, dùng chính `reconcile_ingest_run` đó. Nên hàng ở
    đây đã có người dọn rồi.

    Điều đó làm đường này thành đường ĐỌC THUẦN, và giữ đúng như thế là có giá
    trị: một lần đối chiếu ở đây sẽ là chỗ THỨ HAI đẩy trạng thái pipeline, chạy
    dưới quyền NGƯỜI ĐỌC thay vì `run_as` — tức là một người chỉ có `item.read`
    lại làm thay đổi hàng trong database bằng cách mở một trang lên xem. Và nó
    sẽ đua với tick: hai bộ đối chiếu cùng lúc trên một hàng là đúng thứ
    `reconcile_ingest_run` được viết ra để chỉ có một bản.

    Cái giá, nói ra chứ không giấu: nếu `loom-scheduler` chết, các run đang chạy
    đứng im ở `running` và đường này trung thành báo lại `running` — không có
    timeout nào ở đây biến chúng thành `failed`. Đó là câu trả lời ĐÚNG (không
    ai biết chúng đã chết), và chỗ chữa là giám sát scheduler, không phải đoán
    mò trong một handler đọc.

    Cổng quyền, và vì sao nó gắn vào item pipeline: xem docstring module.
    """
    run = (
        await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pipeline run with this id")
    # `run.pipeline_id` — TỪ HÀNG, không bao giờ từ client. Client chỉ đưa
    # `run_id`; mọi thứ khác được tra ra.
    await PermissionService(session, principal).require_item(run.pipeline_id, Action.item_read)

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

    return PipelineRunDetail(
        **_summary(run).model_dump(),
        run_as_user_id=run.run_as_user_id,
        steps=[_step_out(step) for step in steps],
    )
