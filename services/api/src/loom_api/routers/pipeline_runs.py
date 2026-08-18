"""BA đường ĐỌC cho các lần chạy được lập lịch của một pipeline:

- `GET /api/v1/pipelines/{pipeline_id}/runs` — danh sách run của MỘT pipeline,
  mới nhất trước, phân trang bằng cursor.
- `GET /api/v1/pipeline-runs` — danh sách run XUYÊN pipeline, gác bằng
  `visible_items_select`. Nền của Monitor Hub (Giai đoạn 3c).
- `GET /api/v1/pipeline-runs/{run_id}` — MỘT run kèm cả chuỗi bước của nó.

Cả ba chỉ ĐỌC. Không có đường nào ở đây tạo, huỷ hay chạy lại một run: nguồn
duy nhất sinh ra `pipeline_run` là nhịp lịch (`routers/internal_schedule.py`),
và mở một đường thứ hai là mở một chỗ thứ hai để quyết định "nhịp này có được
chạy không" — đúng câu hỏi mà `schedule_service.decide` là nơi duy nhất trả lời.

## Vì sao ba đường này tồn tại

Trước chúng, cách DUY NHẤT để biết một pipeline đã chạy ra sao là hỏi thẳng
Postgres từ trong pod `loom-api`. Điều đó làm hai thứ không làm được: `make
smoke` chỉ nói HTTP (nó cố ý không dùng `kubectl` — xem đầu `scripts/smoke.sh`),
nên không có phép kiểm chấp nhận nào che được đường lập lịch; và Monitor Hub của
Giai đoạn 3c không có gì để vẽ.

Hình dạng vì thế nhắm vào cả hai người dùng đó: một DANH SÁCH để quét trạng thái
(một dòng một run, không kèm bước — xem `PipelineRunSummary`), và một CHI TIẾT
để mở đúng một run ra xem nó chết ở bước nào.

Đường XUYÊN pipeline đến sau, ở 3c, cho một câu hỏi mà hai đường trên không trả
lời được — xem docstring `list_all_pipeline_runs`.

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
đây là `pipeline_run.pipeline_id`. Cùng một luật, cùng một `PermissionService`.

Hai HÌNH THỨC gác, không hai bộ QUY TẮC — và chỗ khác biệt này đáng nói rõ, vì
đọc nhanh sẽ tưởng là mâu thuẫn. Hai đường hỏi về một id cụ thể gọi
`require_item` cho đúng hàng đó; đường danh sách xuyên pipeline không có id nào
để hỏi (nó phải LỌC, và hỏi từng hàng một là N+1 lượt truy vấn cho mỗi trang),
nên nó gác bằng biểu thức `visible_items_select`. Cả hai dựng trên CÙNG
`_chain_conditions` và CÙNG `_roles_allowing(Action.item_read)` trong
`permissions.py` — nên không có quy tắc quyền nào được viết trong file này, và
không có chỗ nào ở đây để hai đường trôi khỏi nhau.

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
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.models import Item, PipelineRun, PipelineStepRun
from loom_api.pagination import Page, decode_cursor, encode_cursor
from loom_api.permissions import PermissionService, visible_items_select
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

# Trạng thái hợp lệ ở ĐẦU VÀO. `Literal` ở đây, `str` ở đầu ra — và hai điều đó không
# mâu thuẫn: đầu ra dựng từ dữ liệu ĐÃ LƯU nên `Literal` biến một hàng lạ thành 500
# (xem docstring `PipelineStepRunOut`), còn đầu vào đến từ CLIENT nên `Literal` biến một
# giá trị gõ sai thành 422 kèm danh sách giá trị hợp lệ — thay vì một trang RỖNG mà
# người dùng không phân biệt được với "không có run nào hỏng". Cùng lập luận
# `ItemCreate.type` đã ghi.
RunStatusFilter = Literal["pending", "running", "succeeded", "failed", "skipped"]


def visible_pipeline_runs_select(principal: Principal) -> Select[tuple[PipelineRun]]:
    """`select(PipelineRun)` đã giới hạn vào các pipeline người gọi ĐƯỢC ĐỌC.

    Nền là `visible_items_select` — biểu thức DUY NHẤT trong repo trả lời "principal
    này thấy item nào". Nó JOIN `Workspace` và lọc `state == ACTIVE` ở CẢ HAI bảng,
    rồi mở chuỗi tổ tiên item → workspace → domain → tenant. Một `WHERE` viết tay ở
    đây sẽ là bộ lọc THỨ HAI cho cùng một câu hỏi, và hai bộ lọc cho một câu hỏi là
    hai chỗ để lệch: 3b đã lọc `Item.state` mà quên `Workspace.state`, nên workspace
    đã xoá mềm vẫn phóng Job mãi mãi.

    Là một hàm có TÊN chứ không viết thẳng trong handler, cùng lý do
    `search_items_select` tồn tại: test đối chiếu gọi ĐÚNG hàm này. Một test dựng lại
    biểu thức bằng tay chỉ chứng minh rằng bản dựng lại đó đúng.

    Lọc `type` bằng một `.where()` NỐI VÀO, không phải một select mới — bộ lọc chỉ thu
    hẹp, không bao giờ mở rộng quyền.

    `JOIN` vào một subquery chứ không `pipeline_id.in_(...)`: hai cách cho cùng một
    tập hàng, và `JOIN` nhân hàng nếu bên phải có id trùng — ở đây nó KHÔNG, vì
    `item.id` là khoá chính và điều kiện quyền bên trong là một `EXISTS` tương quan
    (không phải một `JOIN role_assignment`, thứ sẽ cho một hàng mỗi quyền khớp).
    Canh bằng `test_a_run_appears_once_when_two_grants_both_match_it`, nên đổi
    `EXISTS` đó thành `JOIN` sẽ đỏ ở đây chứ không âm thầm đếm sai trên Hub.

    Bất đối xứng, nói ra chứ không giấu: `visible_items_select` lọc `state == ACTIVE`,
    nên đường này KHÔNG trả run của pipeline đã xoá mềm, trong khi
    `GET /pipelines/{id}/runs` thì có. Đó là đánh đổi có ý — Hub là màn hình quét cái
    đang chạy, và một pipeline đã xoá không còn trong tập đó; ai cần lịch sử của nó vẫn
    đi được đường theo-id. Test đối chiếu phải khẳng định đúng bất đối xứng này, không
    khẳng định đẳng thức.
    """
    visible = visible_items_select(principal).where(Item.type == str(ItemType.pipeline)).subquery()
    return select(PipelineRun).join(visible, visible.c.id == PipelineRun.pipeline_id)


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


@router.get("/pipeline-runs", response_model=PageOut)
async def list_all_pipeline_runs(
    # `run_status` + `alias="status"`: hợp đồng HTTP vẫn là `?status=`, nhưng tên tham
    # số Python KHÔNG được là `status` — module này `from fastapi import ... status` và
    # dùng `status.HTTP_404_NOT_FOUND` ở hai handler khác. Một tham số tên `status` che
    # mất module đó trong phạm vi hàm, và người thêm một 404 vào đây sau này sẽ nhận
    # `AttributeError: 'str' object has no attribute 'HTTP_404_NOT_FOUND'` — một lỗi
    # không nói gì về nguyên nhân thật.
    #
    # `Annotated[...]` chứ không `= Query(default=None, ...)` như `search.py` viết, và
    # khác biệt đó KHÔNG phải sở thích: ruff miễn B008 ("gọi hàm trong giá trị mặc
    # định") cho tham số FastAPI, nhưng phép miễn đó không nhận ra chú thích là một
    # BÍ DANH kiểu — `RunStatusFilter | None = Query(...)` bị B008 chặn trong khi
    # `q: str = Query(...)` ở `search.py` thì không. Dạng `Annotated` không đặt lời gọi
    # nào vào chỗ mặc định nên nó không phụ thuộc phép miễn đó, và nó cũng là dạng
    # `roles.py` đang dùng (`RevokeQuery`).
    run_status: Annotated[RunStatusFilter | None, Query(alias="status")] = None,
    workspace_id: uuid.UUID | None = None,
    since: datetime | None = None,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> PageOut:
    """Run của MỌI pipeline người gọi được đọc — nền của Monitor Hub (Giai đoạn 3c).

    Vì sao nó tồn tại bên cạnh `GET /pipelines/{id}/runs`: hai câu hỏi khác nhau và
    không thay nhau được. Người soạn pipeline hỏi "pipeline của tôi chạy ra sao";
    người trực hỏi "có gì hỏng đêm qua". Bắt người trực đi qua mười hai trang pipeline
    để trả lời câu thứ hai là không trả lời được.

    Cổng quyền: `visible_pipeline_runs_select`, xem docstring của nó. KHÔNG có phép
    kiểm quyền thứ hai trong handler này.

    KHÔNG kèm `display_name` của pipeline hay tên workspace. Hub cần tên để vẽ, nhưng
    lấy chúng ở đây là JOIN thêm và sinh một kiểu phản hồi THỨ HAI cho cùng một hàng.
    Giao diện đã có `useItems`/`useWorkspaces` trong cache và giải tên ở client.

    Sắp và phân trang trên `(scheduled_for, id)` giảm dần — cùng khoá và cùng lý do mà
    `list_pipeline_runs` ghi: `scheduled_for` là MỐC NHỊP (thứ tự người ta nghĩ về các
    lần chạy) và nó ổn định, còn `started_at` là lúc tick nhìn thấy nhịp nên một tick
    chậm làm hai run đảo chỗ. Cặp chứ không một cột: `UNIQUE (pipeline_id,
    scheduled_for)` chỉ duy nhất TRONG một pipeline, và ở đây nhiều pipeline nằm cùng
    một danh sách — nên `scheduled_for` một mình còn ÍT duy nhất hơn ở đường kia.
    """
    stmt = visible_pipeline_runs_select(principal)

    if since is not None:
        # Chuẩn hoá về UTC TRƯỚC khi làm gì khác, và đây là một lỗi ĐÃ ĐO chứ không
        # phải đề phòng lý thuyết. `?since=2026-08-18` (hay bất kỳ mốc thiếu offset)
        # được Pydantic nhận thành một `datetime` NAIVE, và một datetime naive đưa vào
        # phép so với cột `timestamptz` cho kết quả SAI mà không báo gì: đo trên chính
        # bộ test này, cùng một mốc và `SHOW TimeZone` = UTC, bản naive khớp 2 hàng còn
        # bản có offset khớp 1 — asyncpg dịch mốc naive theo giờ ĐỊA PHƯƠNG của máy
        # chạy API. Nghĩa là trên một máy UTC nó trông đúng, và chỉ lệch khi triển khai
        # ở nơi có offset khác 0; đúng loại sai lặng lẽ mà một trang rỗng-nhưng-hợp-lệ
        # không phân biệt được với "đêm qua không có gì chạy".
        #
        # Coi naive là UTC chứ không trả 422: mọi mốc trong API này đều là UTC
        # (`scheduled_for` trả ra kèm `Z`), nên đó là điều người gọi muốn nói, và nó
        # giữ `?since=2026-08-18` dùng được cho người gõ URL bằng tay.
        #
        # TRƯỚC `filters` chứ không sau: dấu vết cursor phải tính trên giá trị ĐÃ chuẩn
        # hoá, nếu không thì `since=...T00:00:00` và `since=...T00:00:00Z` — cùng một
        # bộ lọc — sinh hai dấu vết khác nhau và cursor của bên này bị bên kia từ chối.
        since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)

    # Bộ lọc đi vào dấu vết cursor: một cursor lấy ở `status=failed` dán sang
    # `status=running` phải là 400, không phải trang thứ hai của một tập KHÁC.
    filters: dict[str, object] = {
        "status": run_status,
        "workspace_id": str(workspace_id) if workspace_id else None,
        "since": since.isoformat() if since else None,
    }

    if run_status is not None:
        stmt = stmt.where(PipelineRun.status == run_status)
    if workspace_id is not None:
        # Lọc THÊM, không thay: `visible_pipeline_runs_select` đã quyết định tập được
        # phép thấy, và một tham số lọc không bao giờ được mở rộng nó.
        stmt = stmt.where(PipelineRun.workspace_id == workspace_id)
    if since is not None:
        stmt = stmt.where(PipelineRun.scheduled_for >= since)

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
