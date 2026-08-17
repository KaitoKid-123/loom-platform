"""Tick endpoint — `loom-scheduler` gọi mỗi N giây.

Đây là HTTP endpoint mà `loom-scheduler` gọi để kích hoạt các pipeline được lập
lịch. Nó phải:

1. Kiểm `X-Loom-Schedule-Secret` (Task 4).
2. Tìm các lịch tới hạn — ĐỌC TỪ `item.definition`, xem "Lịch nằm ở đâu" dưới.
3. Mỗi lịch gọi `decide()` (Task 5) để quyết định start/skip.
4. Kiểm LẠI quyền của `run_as_user_id` (Task 8) — xem "Quyền" dưới.
5. Tạo đúng MỘT `pipeline_run` cho mỗi nhịp (ràng buộc UNIQUE lo phần song song).
6. Đối chiếu bước đang chạy rồi đẩy các run đang dở sang bước tiếp theo (Task 7).
7. Trả nhanh — giới hạn trong `TICK_BUDGET_SECONDS`.

## Vì sao một tick làm HAI việc, và vì sao nó POLL

Không có callback nào báo cho tick biết một bước đã xong, và cả hai loại bước
đều thiếu nó vì hai lý do khác nhau:

- Bước `ingest`: pod nạp báo tiến độ vào hàng `ingest_run` qua
  `/internal/ingest/*`. Nó không biết gì về pipeline — 3a có trước 3b — nên
  `pipeline_step_run` không bao giờ được ai chạm tới từ phía đó.
- Bước `sql`: `loom-query` trả `202 + query_id` rồi im lặng cho tới khi có
  người `GET` (xem docstring `loom_query.routers.query`).

Nên tick tự hỏi: nộp ở một nhịp, hỏi trạng thái ở các nhịp sau. Đó là cả lý do
spec mục 2 gộp hai việc vào một tick thay vì dựng một đường callback thứ ba, và
là lý do tick KHÔNG được đứng chờ — một câu SQL quét vài phút sẽ giữ một request
và một session của pool 3+2 suốt thời gian đó.

Hệ quả nhìn thấy được: một chuỗi `ingest → sql` cần vài nhịp để đi hết, mỗi nhịp
một nấc. Với chu kỳ tick vài chục giây thì đó là độ trễ chấp nhận được cho một
thứ vốn chạy theo cron.

## Lịch nằm ở đâu, và cái giá của chỗ đó

Lịch nằm trong `item.definition` (`PipelineDefinition.schedule`), KHÔNG trong
một bảng riêng — migration 0008 từng tạo bảng `pipeline` với cột `next_run_at`
có index, và 0009 đã bỏ nó. Lý do ở spec mục 4: definition đã được đánh version,
đã có ETag chống ghi đè đồng thời, và Giai đoạn 5 (Git export + deployment rule)
đã giả định definition là đơn vị xuất/nhập. Một bảng bên cạnh không có gì trong
ba thứ đó.

**Cái giá là thật, và nó ở ngay đây:** tìm lịch tới hạn giờ là QUÉT mọi hàng
`item` kiểu `pipeline` rồi parse JSONB của từng hàng, thay vì một lần đọc index
trên `next_run_at`. Ở quy mô hiện tại — một nắm pipeline — chi phí đó không đo
được. Ở quy mô hàng nghìn pipeline thì đo được, và lúc đó cách chữa KHÔNG phải
là dựng lại bảng lịch (mất version/ETag/export) mà là một index biểu thức trên
`(definition -> 'schedule' ->> 'enabled')` cộng một cột sinh cho nhịp kế tiếp.
Ghi ra đây để người gặp vấn đề đó không phải suy luận lại từ đầu.

## "Tới hạn" tính thế nào khi không còn `next_run_at`

Cột `next_run_at` là một MỐC ĐÃ TÍNH SẴN; không còn nó thì mốc phải suy ra được
từ dữ liệu còn lại. Mốc đó là:

    neo = scheduled_for LỚN NHẤT của pipeline này, hoặc item.updated_at nếu chưa
          có run nào
    nhịp kế = next_tick(cron, timezone, neo)
    tới hạn khi nhịp kế <= thời điểm tick

Chọn `MAX(pipeline_run.scheduled_for)` làm neo chứ không phải "nhịp gần nhất
TRƯỚC bây giờ" vì hai điều:

- Nó dùng ĐÚNG `loom_core.cron.next_tick` — nơi DUY NHẤT ba luật DST được viết
  ra (spec mục 5.2). Một hàm `previous_tick` là chỗ thứ hai để ba luật đó trôi.
- Chính hàng `pipeline_run` là bộ nhớ của scheduler, kể cả hàng `skipped`. Nhờ
  `UNIQUE (pipeline_id, scheduled_for)`, một nhịp đã có hàng không bao giờ được
  làm lại — nên neo tiến lên đơn điệu và không nhịp nào chạy hai lần.

Hệ quả có thể nhìn thấy được, nói ra chứ không giấu: sau một quãng scheduler
chết, tick xử lý các nhịp bị lỡ MỖI TICK MỘT NHỊP thay vì nhảy thẳng tới nhịp
mới nhất. Chúng không chồng lên nhau — chốt "không tự giẫm" của `decide()` biến
nhịp thứ hai trở đi thành hàng `skipped` có lý do đọc được, và hàng đó chiếm chỗ
của nhịp nên nó không được thử lại. Đó là chính sách `skip` mà spec mục 7 chọn,
chỉ khác là nó để lại dấu vết thay vì im lặng.

Neo dự phòng `item.updated_at` chỉ dùng cho pipeline CHƯA từng chạy. Nó có một
tính chất tốt: bật lịch lúc 10:30 với cron hằng giờ KHÔNG sinh ngay một run cho
nhịp 10:00 đã qua.

## Quyền: kiểm lại ở MỖI nhịp

`run_as_user_id` lấy từ `ScheduleDefinition`, KHÔNG từ `created_by`/`updated_by`
của hàng item. Với `created_by`/`updated_by` thì quyền TRÔI theo mỗi lần sửa: một
admin sửa chính tả trong pipeline của một contributor là pipeline đó chạy bằng
quyền admin — không ai định thế và không ai nhìn thấy.

Và quyền được hỏi LẠI ở mỗi nhịp, không phải một lần lúc bật lịch. Người đó nghỉ
việc hay bị thu quyền thì run phải hỏng TO TIẾNG kèm lý do, chứ không lặng lẽ
chạy tiếp bằng một lần cấp phép từ sáu tháng trước. Phép hỏi là
`PermissionService.require_item(lakehouse, Action.item_update)` — ĐÚNG cổng mà
`start_ingest` của 3a đòi, gọi lại chứ không viết lại: Giai đoạn 1 cố ý giữ MỘT
bộ máy luật quyền và có một test đối chiếu canh điều đó (xem `permissions.py`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from time import monotonic

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

from loom_api.deps import SessionDep
from loom_api.ingest_service import SecretRefUnusable, secret_name_for
from loom_api.internal_security import require_schedule_secret
from loom_api.models import (
    ACTIVE,
    AppUser,
    IngestRun,
    Item,
    PipelineRun,
    PipelineStepRun,
    UserSession,
)
from loom_api.permissions import PermissionService
from loom_api.routers.ingest import launch_ingest_job, reconcile_ingest_run
from loom_api.routers.query import query_request
from loom_api.schedule_service import decide
from loom_core.config import Settings
from loom_core.cron import CronInvalid, TimezoneInvalid, next_tick
from loom_core.item_definitions import (
    ConnectionDefinition,
    ItemType,
    PipelineDefinition,
    PipelineStep,
    ScheduleDefinition,
    SqlStepConfig,
)
from loom_core.roles import Action
from loom_core.schemas import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["internal"], dependencies=[Depends(require_schedule_secret)])

# Một tick là một HTTP request như mọi request khác — không có gì làm nó đặc
# biệt. Một tick chạy năm phút là một request treo năm phút, giữ một session
# trong pool 3+2 suốt thời gian đó, trong khi scheduler vẫn gõ nhịp tiếp.
#
# Trần phải NHỎ HƠN chu kỳ tick, và phần làm dở để tick sau dọn nốt — an toàn vì
# mọi thao tác ở đây đều idempotent: `UNIQUE (pipeline_id, scheduled_for)` chặn
# run trùng, còn việc đẩy bước là đọc-trạng-thái-rồi-quyết chứ không phải một
# máy trạng thái nhớ mình đang ở đâu.
TICK_BUDGET_SECONDS = 20

# Trần đồng thời toàn cục (spec mục 6, chốt 2) — bao nhiêu `pipeline_run` được
# phép ở trạng thái `running` cùng lúc trên cả cụm.
#
# **Con số này CHƯA ĐƯỢC ĐO.** Task 9 đo nó (ràng buộc chặt nhất nhiều khả năng
# là pool 3+2 của `loom-api`, không phải RAM) và thay hằng số này bằng
# `Settings.pipeline_concurrency_cap`. Để 3 ở đây là một chỗ giữ chân, không
# phải một kết luận — dự án này đã trả giá hai lần cho ngưỡng bịa, nên nó được
# ghi là chưa đo thay vì được trình bày như đã đo.
#
# Đếm TOÀN CỤC chứ không theo từng pipeline: theo từng pipeline thì chốt này là
# hệ quả của chốt "không tự giẫm" ngay phía trên nó trong `decide()` và không
# bao giờ chặn thêm hàng nào — một cái chốt chết. Bảng `pipeline` cũ có cột
# `concurrency_cap` riêng cho mỗi pipeline; nó đi cùng bảng, và spec chưa bao
# giờ đòi một trần theo pipeline.
CONCURRENCY_CAP = 3


class TickResponse(BaseModel):
    schedules_processed: int
    runs_started: int
    runs_skipped: int
    # Run hỏng NGAY tại nhịp — hôm nay chỉ có một nguyên nhân: `run_as` không
    # còn quyền. Đếm riêng chứ không gộp vào `runs_skipped`: `skipped` là "đúng
    # theo thiết kế, thử lại nhịp sau", `failed` là "có người phải đi sửa".
    runs_failed: int = 0


# ------------------------------------------------------------------ tra cứu item


async def _active_item(
    session: AsyncSession, item_id: uuid.UUID, item_type: ItemType
) -> Item | None:
    """Hàng `item` đang sống ĐÚNG loại này, hoặc `None`.

    Bản chép có chủ đích của `routers/ingest.py::_active_item`, và docstring ở
    đó đã nêu lý do chấp nhận được: ba điều kiện `id`/`type`/`state` không mã
    hoá luật nào để trôi khỏi nhau. Luật quyền — thứ DUY NHẤT không được có bản
    thứ hai — nằm ở `PermissionService`, và cả hai đường đều gọi nó.
    """
    stmt = select(Item).where(
        Item.id == item_id,
        Item.type == str(item_type),
        Item.state == ACTIVE,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _parsed_pipeline(item: Item) -> PipelineDefinition | None:
    """`PipelineDefinition` của một hàng item, hoặc `None` kèm log nếu không parse được.

    Một definition không parse được là DỮ LIỆU ĐÃ LƯU bị hỏng, không phải một
    request sai — và nó không được kéo theo cả nhịp: một pipeline hỏng làm chết
    tick là mọi pipeline khác ngừng chạy. Nhưng nó cũng không được biến mất
    lặng lẽ, nên nó đi ra log ở mức ERROR. Không có hàng `pipeline_run` nào để
    gắn lỗi vào: chưa parse được thì chưa biết pipeline này có lịch hay không,
    tức là chưa biết `scheduled_for` — mà cột đó là NOT NULL.
    """
    try:
        return PipelineDefinition.model_validate(item.definition)
    except ValidationError as exc:
        logger.error(
            "schedule.pipeline_definition_invalid",
            pipeline_id=str(item.id),
            errors=exc.error_count(),
        )
        return None


# --------------------------------------------------------------- lịch tới hạn


async def _due_at(
    session: AsyncSession,
    item: Item,
    schedule: ScheduleDefinition,
    tick_time: datetime,
) -> datetime | None:
    """Mốc nhịp tới hạn của pipeline này, hoặc `None` nếu chưa tới.

    Neo và lý do chọn neo: xem docstring module.
    """
    anchor = (
        await session.execute(
            select(func.max(PipelineRun.scheduled_for)).where(PipelineRun.pipeline_id == item.id)
        )
    ).scalar() or item.updated_at

    try:
        due = next_tick(schedule.cron, schedule.timezone, anchor)
    except (CronInvalid, TimezoneInvalid) as exc:
        # `ScheduleDefinition` kiểm cả hai ở BIÊN, nên tới được đây nghĩa là
        # hàng JSONB đã bị ghi thẳng vào database mà không đi qua `POST /items`.
        # Log rồi bỏ qua pipeline này — cùng lập luận với `_parsed_pipeline`.
        logger.error(
            "schedule.cron_unusable",
            pipeline_id=str(item.id),
            cron=schedule.cron,
            timezone=schedule.timezone,
            reason=str(exc),
        )
        return None

    return due if due <= tick_time else None


# ------------------------------------------------------------ quyền của run_as


async def _run_as_principal(session: AsyncSession, user_id: uuid.UUID) -> Principal | None:
    """Principal của người mà lịch chạy DƯỚI DANH NGHĨA, hoặc `None` nếu không còn.

    `groups` lấy từ hàng `user_session` MỚI NHẤT của người đó, không phải tuple
    rỗng. Một run được lập lịch không có phiên đăng nhập nào của riêng nó, nên
    câu hỏi "người này thuộc nhóm nào" không có câu trả lời tươi — nhóm vốn được
    CHỤP lúc đăng nhập (xem `UserSession.groups`), kể cả cho đường tương tác.
    Lấy ảnh chụp gần nhất là câu trả lời gần đúng nhất mà hệ thống có; để rỗng
    thì một người chỉ được cấp quyền QUA NHÓM sẽ làm lịch hỏng mỗi nhịp bằng một
    lý do sai — đúng loại tiếng ồn làm người ta ngừng đọc lý do.

    KHÔNG lọc `expires_at`: một người đăng xuất (hay phiên hết hạn) vẫn là nhân
    viên, và lịch của họ không nên hỏng vì điều đó. Thứ PHẢI làm lịch hỏng là
    mất `role_assignment` — và đó chính là thứ `PermissionService` đọc tươi ở
    mỗi nhịp.
    """
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return None

    groups = (
        await session.execute(
            select(UserSession.groups)
            .where(UserSession.user_id == user_id)
            .order_by(UserSession.created_at.desc())
            .limit(1)
        )
    ).scalar()

    return Principal(
        user_id=user.id,
        subject=user.subject,
        email=user.email,
        display_name=user.display_name,
        groups=tuple(groups or ()),
    )


def _target_lakehouses(definition: PipelineDefinition) -> list[uuid.UUID]:
    """Các lakehouse mà chuỗi bước này GHI vào, không trùng lặp, giữ thứ tự.

    Cả `ingest` lẫn `sql` đều mang `lakehouse_id` và cả hai đều GHI (một cái nạp
    vào bronze, một cái dựng silver), nên cả hai phải qua cổng. Kiểm mỗi
    lakehouse một lần là đủ và `PermissionService` còn cache trong phạm vi thực
    thể của nó.
    """
    targets: list[uuid.UUID] = []
    for step in definition.steps:
        config = step.ingest or step.sql
        if config is not None:
            targets.append(config.lakehouse_id)
    return list(dict.fromkeys(targets))


async def _authority_failure(
    session: AsyncSession,
    definition: PipelineDefinition,
    schedule: ScheduleDefinition,
) -> str | None:
    """Lý do run KHÔNG được phép chạy, hoặc `None` khi `run_as` còn đủ quyền.

    Trả một CHUỖI chứ không ném: chuỗi này đi thẳng vào `pipeline_run.error` và
    là thứ người vận hành đọc lúc 9 giờ sáng. Nó phải tự đủ nghĩa (ai, lakehouse
    nào, sửa bằng cách nào) vì không còn gì khác để đọc.
    """
    # `run_as_user_id is None` KHÔNG tới được đây: `_process_tick` loại nó cùng
    # chỗ loại một definition hỏng, vì `pipeline_run.run_as_user_id` là NOT NULL
    # — không có hàng nào ghi được lý do cho một lịch không nêu tên ai.
    assert schedule.run_as_user_id is not None
    principal = await _run_as_principal(session, schedule.run_as_user_id)
    if principal is None:
        return (
            f"the schedule runs as user {schedule.run_as_user_id}, and that user no longer "
            "exists — set run_as_user_id in the pipeline definition to someone who does."
        )

    # MỘT thực thể cho cả vòng lặp: cache của nó có phạm vi bằng đúng phép kiểm
    # này, nên hai bước cùng ghi vào một lakehouse chỉ tốn một round trip.
    perms = PermissionService(session, principal)
    for lakehouse_id in _target_lakehouses(definition):
        try:
            await perms.require_item(lakehouse_id, Action.item_update)
        except HTTPException:
            # `NotVisible` (404) và `Forbidden` (403) đều là `HTTPException`, và
            # ở đây hai cái đó là MỘT câu trả lời: người này không được ghi vào
            # lakehouse đó. Phân biệt chúng chỉ có nghĩa với một client HTTP —
            # còn ở đây không có client nào, chỉ có một hàng lỗi để đọc.
            return (
                f"the schedule runs as {principal.email}, but that user no longer has "
                f"permission to write to lakehouse {lakehouse_id} — the run was not started. "
                "Grant them contributor on that lakehouse, or change run_as_user_id in the "
                "pipeline definition."
            )
    return None


# ------------------------------------------------------------------ chạy bước


def _clipped(text: str, limit: int = 400) -> str:
    """Một dòng, cắt ngắn — thân phản hồi lỗi của một service đi vào cột `error`.

    `pipeline_step_run.error` là `Text` nên không có giới hạn kỹ thuật, nhưng
    một trang HTML lỗi của reverse proxy đổ nguyên vào đó thì cột này không còn
    đọc được bằng mắt, và nó hiện lên giao diện 3c.
    """
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + " …"


async def _fail_step(
    session: AsyncSession,
    run: PipelineRun,
    step_run: PipelineStepRun,
    reason: str,
) -> None:
    """Đánh hỏng một bước VÀ cả run — hỏng to tiếng, không để lại gì đang chạy."""
    now = datetime.now(UTC)
    step_run.status = "failed"
    step_run.started_at = step_run.started_at or now
    step_run.finished_at = now
    step_run.error = reason
    run.status = "failed"
    run.error = reason
    run.finished_at = now
    await session.commit()


async def _succeed_step(session: AsyncSession, step_run: PipelineStepRun) -> None:
    """Đóng một bước ở trạng thái THÀNH CÔNG.

    KHÔNG đụng tới `run.status`: một bước xong không có nghĩa run xong — quyết
    định đó thuộc về `_advance_run`, nơi duy nhất nhìn thấy CẢ chuỗi. Hai chỗ
    cùng đánh `succeeded` cho run là hai chỗ để một run kết thúc sớm khi còn bước
    chưa chạy.
    """
    now = datetime.now(UTC)
    step_run.status = "succeeded"
    step_run.started_at = step_run.started_at or now
    step_run.finished_at = now
    await session.commit()


async def _start_sql_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
    config: SqlStepConfig,
) -> None:
    """Nộp câu SQL của một bước sang `loom-query`, và ghi lại `query_id`.

    **Principal gửi đi là `run.run_as_user_id`, và đó là một quyết định về
    QUYỀN, không phải một trường siêu dữ liệu.** `loom-query` không có OIDC: nó
    nhận principal trong thân request và TIN nó, chỉ chặn bằng bí mật chia sẻ
    (xem `loom_core.internal_auth`). Nên principal ta gửi CHÍNH LÀ thẩm quyền câu
    SQL chạy dưới — `run_gate` của `loom-query` hỏi `/internal/authz/items` bằng
    đúng principal đó. Gửi nhầm ai đó khác không phải một nhãn sai trong log; nó
    là một câu SQL chạy bằng quyền của người không hề bấm nút nào.

    `run.run_as_user_id` chứ không phải người tạo/sửa item: cùng lý do đã ghi ở
    docstring module, và cột đó được ghi từ `ScheduleDefinition` lúc tạo run.

    **`workspace_id` TRA TỪ lakehouse, không bao giờ nhận từ đâu khác** — cùng
    luật mà `routers/query.py` ghi ở docstring của nó: nó là phạm vi phân giải
    tên bảng ba phần, và một phạm vi sai làm cổng quyền chạy trên sai tập item.

    **Hàng TRƯỚC, lời gọi mạng SAU** — cùng thứ tự với bước `ingest`, nhưng lý do
    KHÔNG giống hệt và chỗ khác nhau đáng nói ra. Với `ingest`, phóng lại vô hại
    vì `job_name` tất định theo `run_id`; với SQL thì KHÔNG: nộp lại một câu
    `INSERT` là chèn hai lần. Nên nếu tiến trình chết giữa `commit()` và lời gọi
    `POST`, bước nằm lại ở `running` mà không có `query_id` — và
    `_reconcile_sql_step` đánh HỎNG đúng hình dạng đó thay vì nộp lại. Hỏng to
    tiếng cho một câu SQL có-thể-đã-chạy là hướng đúng của hai hướng.
    """
    lakehouse = await _active_item(session, config.lakehouse_id, ItemType.lakehouse)
    if lakehouse is None:
        await _fail_step(
            session,
            run,
            step_run,
            f"this step writes to lakehouse {config.lakehouse_id}, which no longer exists",
        )
        return

    principal = await _run_as_principal(session, run.run_as_user_id)
    if principal is None:
        # `_authority_failure` đã hỏi đúng câu này lúc run được TẠO, nhưng bước
        # SQL thường bắt đầu ở một tick SAU đó — và người kia có thể đã bị xoá
        # trong khoảng giữa. Không có principal thì không có thẩm quyền nào để
        # chạy câu SQL dưới danh nghĩa, và chạy nó dưới danh nghĩa "không ai" là
        # đúng thứ không được phép xảy ra.
        await _fail_step(
            session,
            run,
            step_run,
            f"this run runs as user {run.run_as_user_id}, and that user no longer exists — "
            "the SQL was not submitted. Set run_as_user_id in the pipeline definition to "
            "someone who does.",
        )
        return

    now = datetime.now(UTC)
    step_run.status = "running"
    step_run.started_at = now
    run.status = "running"
    await session.commit()

    try:
        response = await query_request(
            app_state,
            settings,
            method="POST",
            path="/query",
            json_body={
                "lakehouse_id": str(config.lakehouse_id),
                "workspace_id": str(lakehouse.workspace_id),
                "sql": config.sql,
                "principal": principal.model_dump(mode="json"),
            },
        )
    except httpx.HTTPError as exc:
        await _fail_step(
            session,
            run,
            step_run,
            f"loom-query could not be reached to run this step's SQL ({exc}); nothing was run.",
        )
        return

    if response.status_code != 202:
        # Thân phản hồi của `loom-query` đã đúng hình dạng người đọc cần (400 kèm
        # dòng/cột SQL, 403 "thiếu quyền" — xem docstring `routers/query.py`),
        # nên nó đi thẳng vào `error` thay vì bị dịch lại qua một câu chung chung.
        await _fail_step(
            session,
            run,
            step_run,
            f"loom-query refused this step's SQL with HTTP {response.status_code}: "
            f"{_clipped(response.text)}",
        )
        return

    query_id = _query_id_of(response)
    if query_id is None:
        await _fail_step(
            session,
            run,
            step_run,
            "loom-query accepted this step's SQL but its reply carried no query_id, so there "
            f"is nothing to poll: {_clipped(response.text)}",
        )
        return

    step_run.query_id = query_id
    await session.commit()


def _query_id_of(response: httpx.Response) -> str | None:
    """`query_id` trong thân một phản hồi 202, hoặc `None` nếu không đọc được.

    Không để `response.json()` ném lên: một `loom-query` trả 202 kèm thân không
    phải JSON (một proxy chen vào giữa, chẳng hạn) sẽ làm CẢ nhịp tick hỏng —
    tức là mọi pipeline khác ngừng chạy vì một bước của một pipeline.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    query_id = body.get("query_id")
    return str(query_id) if query_id else None


async def _start_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
    step: PipelineStep,
) -> None:
    """Khởi động một bước: hàng Postgres TRƯỚC, việc thật SAU.

    Thứ tự đó là thứ tự của `start_ingest` và không đảo lại được — lý do đầy đủ
    ở docstring `routers/ingest.py`: hàng là *ý định*, Job chỉ là cách ý định
    thành sự thật, và `job_name` tất định theo `run_id` làm việc phóng lại vô
    hại. Đảo lại thì một commit hỏng để lại một pod đang chạy đi hỏi spec của
    một run không tồn tại.

    Bước `sql` đi qua `_start_sql_step` (nộp sang `loom-query`); chỗ nó KHÁC
    bước `ingest` — nộp lại một câu SQL KHÔNG vô hại — ghi ở docstring hàm đó.
    """
    now = datetime.now(UTC)

    if step.type == "sql":
        if step.sql is None:
            # `PipelineStep._config_matches_type` chặn ở biên; ở đây là lớp phòng
            # vệ cho JSONB ghi thẳng — cùng lập luận với nhánh `ingest` dưới.
            await _fail_step(session, run, step_run, "this SQL step carries no sql configuration")
            return
        await _start_sql_step(session, app_state, settings, run, step_run, step.sql)
        return

    config = step.ingest
    if config is None:
        # `PipelineStep._config_matches_type` chặn ở biên; ở đây là lớp phòng vệ
        # cho JSONB ghi thẳng, và nó hỏng to tiếng thay vì bỏ qua bước.
        await _fail_step(session, run, step_run, "this ingest step carries no ingest configuration")
        return

    lakehouse = await _active_item(session, config.lakehouse_id, ItemType.lakehouse)
    if lakehouse is None:
        await _fail_step(
            session,
            run,
            step_run,
            f"this step writes to lakehouse {config.lakehouse_id}, which no longer exists",
        )
        return

    connection = await _active_item(session, config.connection_id, ItemType.connection)
    if connection is None:
        await _fail_step(
            session,
            run,
            step_run,
            f"this step reads through connection {config.connection_id}, which no longer exists",
        )
        return

    try:
        connection_definition = ConnectionDefinition.model_validate(connection.definition)
        secret_name = secret_name_for(connection_definition.secret_ref, settings.task_namespace)
    except (ValidationError, SecretRefUnusable) as exc:
        await _fail_step(
            session,
            run,
            step_run,
            f"this step cannot use connection {config.connection_id}: {exc}",
        )
        return

    ingest_run = IngestRun(
        id=uuid.uuid4(),
        lakehouse_id=config.lakehouse_id,
        connection_id=config.connection_id,
        # `workspace_id` lấy TỪ LAKEHOUSE, cùng quy tắc `start_ingest` dùng.
        workspace_id=lakehouse.workspace_id,
        stream=config.stream,
        mode=config.mode,
        status="pending",
    )
    session.add(ingest_run)
    # Bước NỐI vào hàng `ingest_run` chứ không chép trạng thái sang: một trạng
    # thái ở hai chỗ là hai chỗ để lệch (spec mục 3).
    step_run.ingest_run_id = ingest_run.id
    step_run.status = "running"
    step_run.started_at = now
    run.status = "running"
    await session.commit()

    # `launch()` ĐỒNG BỘ và chặn (client `kubernetes` dùng urllib3, không có bản
    # async), nên nó đi qua thread — cùng cơ chế `start_ingest` dùng.
    await asyncio.to_thread(launch_ingest_job, app_state, settings, ingest_run.id, secret_name)


# ----------------------------------------------------------- một nhịp tới hạn


async def _start_run(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    definition: PipelineDefinition,
) -> None:
    """Hiện thực hoá các bước của một run vừa được tạo, rồi khởi động bước 0."""
    if not definition.steps:
        # Không có bước nào để chạy. Đánh xong ngay thay vì để một run rỗng nằm
        # `pending` vĩnh viễn và chặn nhịp sau bằng chốt "không tự giẫm".
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        return

    step_runs = [
        PipelineStepRun(
            id=uuid.uuid4(),
            pipeline_run_id=run.id,
            step_index=index,
            step_type=step.type,
            status="pending",
        )
        for index, step in enumerate(definition.steps)
    ]
    session.add_all(step_runs)
    await session.flush()

    await _start_step(session, app_state, settings, run, step_runs[0], definition.steps[0])


async def _process_due_pipeline(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    item: Item,
    definition: PipelineDefinition,
    schedule: ScheduleDefinition,
    due_at: datetime,
) -> tuple[str, uuid.UUID | None]:
    """Xử lý MỘT nhịp tới hạn.

    Trả kết cục (`"started"` / `"skipped"` / `"failed"` / `"raced"`) VÀ id của
    run nếu nhịp này vừa khởi động một cái. Id đó dùng để đường đẩy bước bỏ qua
    chính nó trong cùng một nhịp — xem `_advance_all_running_runs`.
    """
    active_run = (
        (
            await session.execute(
                select(PipelineRun).where(
                    PipelineRun.pipeline_id == item.id,
                    PipelineRun.status == "running",
                )
            )
        )
        .scalars()
        .first()
    )
    concurrent_runs = (
        await session.execute(
            select(func.count(PipelineRun.id)).where(PipelineRun.status == "running")
        )
    ).scalar() or 0

    decision = decide(
        due_at=due_at,
        has_active_run=active_run is not None,
        active_run_started_at=active_run.started_at if active_run else None,
        concurrent_runs=concurrent_runs,
        concurrency_cap=CONCURRENCY_CAP,
    )

    # Quyền hỏi SAU khi đã quyết định start: một nhịp bị bỏ vì run trước còn
    # chạy thì không có gì để cấp phép, và hỏi quyền cho nó chỉ là một round
    # trip thừa mỗi nhịp trên đúng những pipeline chạy lâu nhất.
    authority_error = (
        await _authority_failure(session, definition, schedule)
        if decision.action == "start"
        else None
    )

    if decision.action == "skip":
        status, skip_reason, error = "skipped", decision.reason, None
    elif authority_error is not None:
        status, skip_reason, error = "failed", None, authority_error
    else:
        status, skip_reason, error = "pending", None, None

    # `RETURNING` là thứ phân biệt "ta vừa tạo hàng này" với "một tick khác đã
    # tạo nó trước". Với `ON CONFLICT DO NOTHING`, một va chạm KHÔNG trả về hàng
    # nào — nên nhánh khởi động bên dưới (thứ có tác dụng phụ: phóng một Job)
    # chỉ chạy cho tick THẮNG. Không có `RETURNING`, hai tick song song cùng
    # thấy "xong" và cùng phóng Job cho cùng một nhịp; ràng buộc UNIQUE chỉ chặn
    # được hàng thứ hai, không chặn được pod thứ hai.
    statement = (
        pg_insert(PipelineRun)
        .values(
            id=uuid.uuid4(),
            pipeline_id=item.id,
            workspace_id=item.workspace_id,
            scheduled_for=due_at,
            status=status,
            skip_reason=skip_reason,
            error=error,
            finished_at=datetime.now(UTC) if status == "failed" else None,
            run_as_user_id=schedule.run_as_user_id,
        )
        .on_conflict_do_nothing(constraint="uq_pipeline_run_pipeline_scheduled_for")
        .returning(PipelineRun.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()
    if created_id is None:
        return "raced", None

    if status == "failed":
        logger.warning(
            "schedule.run_as_lost_permission",
            pipeline_id=str(item.id),
            run_as_user_id=str(schedule.run_as_user_id),
            scheduled_for=due_at.isoformat(),
        )
        await session.commit()
        return "failed", None

    if status == "skipped":
        await session.commit()
        return "skipped", None

    run = (
        await session.execute(select(PipelineRun).where(PipelineRun.id == created_id))
    ).scalar_one()
    await _start_run(session, app_state, settings, run, definition)
    # `_start_run` có thể đã đánh hỏng run ngay tại bước 0 (connection biến mất,
    # `secret_ref` không dùng được ở cụm này). Đọc lại trạng thái THẬT thay vì
    # báo "started" cho một run đã `failed` — con số trả về là thứ người vận
    # hành nhìn để biết đêm qua có gì chạy.
    if run.status == "failed":
        return "failed", None
    return "started", created_id


async def _process_tick(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    tick_time: datetime,
) -> TickResponse:
    """Tìm lịch tới hạn, quyết định start/skip/fail, tạo hàng `pipeline_run`."""
    deadline = monotonic() + TICK_BUDGET_SECONDS

    pipelines = (
        (
            await session.execute(
                select(Item).where(
                    Item.type == str(ItemType.pipeline),
                    Item.state == ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )

    counts = {"started": 0, "skipped": 0, "failed": 0, "raced": 0}
    schedules_processed = 0
    started_now: set[uuid.UUID] = set()

    for item in pipelines:
        if monotonic() >= deadline:
            # Hết ngân sách — phần còn lại để tick sau. An toàn vì mọi thao tác
            # ở đây idempotent; xem `TICK_BUDGET_SECONDS`.
            logger.info("schedule.tick_budget_exhausted", processed=schedules_processed)
            break

        definition = _parsed_pipeline(item)
        if definition is None:
            continue
        schedule = definition.schedule
        if schedule is None or not schedule.enabled:
            continue
        if schedule.run_as_user_id is None:
            # `ScheduleDefinition._enabled_names_its_principal` chặn ở biên, nên
            # tới được đây là JSONB đã bị ghi thẳng vào database. Không có hàng
            # `pipeline_run` nào ghi được lý do — `run_as_user_id` là NOT NULL —
            # nên log ERROR là chỗ duy nhất nói được, và lịch KHÔNG chạy.
            logger.error("schedule.enabled_without_run_as", pipeline_id=str(item.id))
            continue
        due_at = await _due_at(session, item, schedule, tick_time)
        if due_at is None:
            continue

        schedules_processed += 1
        outcome, started_id = await _process_due_pipeline(
            session, app_state, settings, item, definition, schedule, due_at
        )
        counts[outcome] += 1
        if started_id is not None:
            started_now.add(started_id)

    await _advance_all_running_runs(session, app_state, settings, deadline, started_now)

    await session.commit()
    return TickResponse(
        schedules_processed=schedules_processed,
        runs_started=counts["started"],
        runs_skipped=counts["skipped"],
        runs_failed=counts["failed"],
    )


# ------------------------------------------------------------- đẩy run đang chạy


async def _advance_all_running_runs(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    deadline: float,
    started_now: set[uuid.UUID],
) -> None:
    """Đối chiếu rồi đẩy MỌI `pipeline_run` đang chạy — nửa thứ hai của một tick.

    `started_now` là các run mà CHÍNH nhịp này vừa khởi động. Bỏ qua chúng: bước
    0 của chúng bắt đầu cách đây vài mili giây, nên hỏi Kubernetes (hoặc
    `loom-query`) về nó là một round trip mà câu trả lời đã biết trước. Chúng
    được đối chiếu ở tick SAU, đúng nhịp polling mà cả đường này dựa vào.

    `deadline` là CÙNG trần với vòng lặp lịch ở trên, không phải một trần thứ
    hai: một tick chạy quá lâu là một request treo giữ một session của pool 3+2,
    bất kể nửa nào của nó tiêu thời gian. Phần bỏ dở được tick sau nhặt lại —
    an toàn vì mọi thao tác ở đây đọc-trạng-thái-rồi-quyết chứ không phải một
    máy trạng thái nhớ mình đang ở đâu.
    """
    running_runs = (
        (await session.execute(select(PipelineRun).where(PipelineRun.status == "running")))
        .scalars()
        .all()
    )

    for run in running_runs:
        if run.id in started_now:
            continue
        if monotonic() >= deadline:
            logger.info("schedule.advance_budget_exhausted", run_id=str(run.id))
            break
        await _advance_run(session, app_state, settings, run)


async def _advance_run(
    session: AsyncSession, app_state: State, settings: Settings, run: PipelineRun
) -> None:
    """Đẩy MỘT `pipeline_run` một nấc: đối chiếu bước đang chạy, rồi quyết định.

    Hai thì, và thứ tự giữa chúng là điều làm chuỗi đi được:

    1. **Đối chiếu** bước đang `running` với nguồn sự thật của nó — hàng
       `ingest_run` (và Job của nó) cho bước nạp, `loom-query` cho bước SQL.
       Không có thì này, không gì trên đời chuyển một bước ra khỏi `running`:
       không có callback nào từ `loom-query`, và pod nạp báo vào `ingest_run`
       chứ không vào `pipeline_step_run`. Chuỗi đứng im vĩnh viễn.
    2. **Quyết định** dựa trên chuỗi SAU khi đối chiếu — bước đầu tiên chưa
       `succeeded` nói tất cả:

       - `failed` → run `failed`, và các bước còn lại NẰM YÊN ở `pending`. Đây
         là chốt quan trọng nhất của cả hàm: một câu SQL dựng silver chạy trên
         bronze nạp dở cho ra một bảng SAI mà KHÔNG lỗi nào báo ra — đúng loại
         hỏng không ai phát hiện, và tệ hơn hẳn một run dừng lại to tiếng.
       - `running` → chưa tới lượt ai cả, chờ tick sau.
       - `pending` → khởi động nó.
       - không còn bước nào chưa `succeeded` → run `succeeded`.

    Quét TUYẾN TÍNH theo `step_index` chứ không tìm "bước cuối đã kết thúc rồi
    mở bước kế". Cách sau có một lỗ đã có thật trong bản trước: bước 0
    `succeeded` + bước 1 `running` cho ra "bước kế = bước 1" và khởi động LẠI
    một bước đang chạy — tức là hàng `ingest_run` thứ hai và một Job thứ hai
    cho cùng một bước, MỖI TICK. Quét tuyến tính không có hình dạng đó: bước
    đầu tiên chưa `succeeded` là bước 1, nó đang `running`, và câu trả lời là
    không làm gì.

    Mỗi tick, mỗi run: NHIỀU NHẤT một lần đối chiếu và NHIỀU NHẤT một lần khởi
    động bước. Hai việc đó xảy ra trong CÙNG một tick khi lần đối chiếu vừa đóng
    một bước và bước kế đang `pending` — đó là điều mong muốn (không phải đợi
    thêm một nhịp cho một chuỗi đã sẵn sàng đi tiếp), và nó vẫn có trần: một run
    mười bước không thể chạy hết trong một tick, nên nó không ăn hết ngân sách
    của cả nhịp.
    """
    steps = list(
        (
            await session.execute(
                select(PipelineStepRun)
                .where(PipelineStepRun.pipeline_run_id == run.id)
                .order_by(PipelineStepRun.step_index)
            )
        )
        .scalars()
        .all()
    )
    if not steps:
        # Một run `running` không có bước nào. `_start_run` đánh `succeeded` ngay
        # cho một definition rỗng, nên hình dạng này chỉ đến từ hàng ghi thẳng
        # vào database. Không đối chiếu được gì, và ĐOÁN ở đây (succeeded? failed?)
        # là bịa ra một kết cục — để nguyên và không làm gì.
        return

    for step_run in steps:
        if step_run.status == "running":
            await _reconcile_step(session, app_state, settings, run, step_run)
            # Đúng một bước chạy tại một thời điểm trong chuỗi tuyến tính này.
            break

    for step_run in steps:
        if step_run.status == "succeeded":
            continue
        if step_run.status == "failed":
            if run.status != "failed":
                # `_fail_step` (đường thường) đã đánh cả hai; tới được đây nghĩa
                # là bước hỏng từ một nhịp trước mà run chưa được đóng.
                run.status = "failed"
                run.error = run.error or step_run.error
                run.finished_at = run.finished_at or datetime.now(UTC)
                await session.commit()
            return
        if step_run.status == "running":
            return
        await _start_indexed_step(session, app_state, settings, run, step_run)
        return

    run.status = "succeeded"
    run.finished_at = datetime.now(UTC)
    await session.commit()


async def _reconcile_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
) -> None:
    """Hỏi nguồn sự thật của một bước đang `running` xem nó xong chưa."""
    if step_run.step_type == "ingest":
        await _reconcile_ingest_step(session, app_state, settings, run, step_run)
    else:
        await _reconcile_sql_step(session, app_state, settings, run, step_run)


async def _reconcile_ingest_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
) -> None:
    """Đóng một bước nạp theo hàng `ingest_run` của nó — KHÔNG có bộ đối chiếu thứ hai.

    Trạng thái thật nằm ở `ingest_run`, và bước chỉ NỐI vào đó (spec mục 3: một
    trạng thái ở hai chỗ là hai chỗ để lệch). Câu hỏi "hàng đó còn đường sống
    không" đã có đúng một câu trả lời trong repo — `reconcile_ingest_run` ở
    `routers/ingest.py`, cùng hàm mà `GET /api/v1/ingest/{run_id}` gọi. Viết một
    bản thứ hai ở đây là mở ra chỗ để hai bên trôi khỏi nhau, và trôi ở đây là
    một pipeline treo hoặc một pipeline báo thành công nó chưa từng có.

    Cổng "không hỏi Kubernetes về một run đã kết thúc" nằm TRONG hàm đó, nên
    đường này không chép lại nó — xem docstring `reconcile_ingest_run`.
    """
    if step_run.ingest_run_id is None:
        await _fail_step(
            session,
            run,
            step_run,
            "this ingest step is marked running but no ingest run is attached to it, so there "
            "is nothing to follow — loom-api was interrupted while starting the step. Nothing "
            "was ingested; start the pipeline again.",
        )
        return

    ingest_run = (
        await session.execute(select(IngestRun).where(IngestRun.id == step_run.ingest_run_id))
    ).scalar_one_or_none()
    if ingest_run is None:
        await _fail_step(
            session,
            run,
            step_run,
            f"the ingest run {step_run.ingest_run_id} this step points at no longer exists, so "
            "how far it got cannot be known. Treat the target table as incomplete and start "
            "the pipeline again.",
        )
        return

    await reconcile_ingest_run(session, app_state, settings, ingest_run)

    if ingest_run.status == "succeeded":
        await _succeed_step(session, step_run)
    elif ingest_run.status == "failed":
        # Lý do THẬT của hàng `ingest_run` (pod tự báo qua `/complete`, hoặc câu
        # đối chiếu viết ra) đi thẳng lên bước. Dịch lại thành một câu chung
        # chung ở đây là vứt đúng dòng người vận hành cần đọc.
        await _fail_step(
            session,
            run,
            step_run,
            ingest_run.error or "the ingest for this step failed without recording a reason",
        )
    # `pending`/`running`: pod còn sống (hoặc Job còn đang khởi động). ĐỂ NGUYÊN
    # — cùng mặc định mà `failure_from_job` chọn và cùng lý do: để một bước chết
    # nằm thêm một tick là chậm, đánh hỏng một bước đang sống là giết việc thật.


async def _reconcile_sql_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
) -> None:
    """Hỏi `loom-query` xem câu SQL của bước này xong chưa.

    POLLING, không callback: `loom-query` trả `202 + query_id` rồi KHÔNG gọi lại
    (xem docstring `loom_query.routers.query`). Tick nộp ở một nhịp và hỏi ở các
    nhịp sau — đó chính là lý do spec mục 2 bắt một tick làm hai việc. Tick
    KHÔNG được đứng chờ câu SQL: một câu quét vài phút sẽ giữ một request và một
    session của pool 3+2 suốt thời gian đó.
    """
    if step_run.query_id is None:
        # Hình dạng của một tiến trình chết giữa `commit()` và lời gọi `POST` ở
        # `_start_sql_step`. Không nộp lại: câu SQL CÓ THỂ đã chạy, và chèn hai
        # lần là làm hỏng dữ liệu bằng một phỏng đoán. Hỏng to tiếng.
        await _fail_step(
            session,
            run,
            step_run,
            "this SQL step is marked running but carries no query_id, so there is nothing to "
            "poll — loom-api was interrupted between marking the step started and submitting "
            "the SQL. The SQL may or may not have run; check the target table before starting "
            "the pipeline again.",
        )
        return

    try:
        response = await query_request(
            app_state, settings, method="GET", path=f"/query/{step_run.query_id}"
        )
    except httpx.HTTPError as exc:
        # `loom-query` không với tới được lúc này. ĐỂ NGUYÊN bước ở `running` và
        # hỏi lại ở tick sau: một lần restart pod (hoặc một giây mạng xấu) không
        # phải bằng chứng câu SQL đã hỏng, và đánh hỏng ở đây sẽ giết một câu SQL
        # đang chạy tốt. Nếu `loom-query` mất hẳn trạng thái thì nhánh 404 dưới
        # đây đóng bước lại — nên "để nguyên" không phải một đường treo vô hạn.
        logger.warning(
            "schedule.query_status_unreachable",
            pipeline_run_id=str(run.id),
            step_index=step_run.step_index,
            query_id=step_run.query_id,
            reason=str(exc),
        )
        return

    if response.status_code == 404:
        await _fail_step(
            session,
            run,
            step_run,
            f"loom-query no longer knows query {step_run.query_id} — it keeps query state in "
            "memory only, so a restart loses every query in flight. Whether the SQL finished "
            "is unknown; check the target table before starting the pipeline again.",
        )
        return

    if response.status_code != 200:
        logger.warning(
            "schedule.query_status_unreadable",
            pipeline_run_id=str(run.id),
            step_index=step_run.step_index,
            query_id=step_run.query_id,
            status_code=response.status_code,
        )
        return

    try:
        body = response.json()
    except ValueError:
        logger.warning(
            "schedule.query_status_not_json",
            pipeline_run_id=str(run.id),
            step_index=step_run.step_index,
            query_id=step_run.query_id,
        )
        return

    query_status = body.get("status") if isinstance(body, dict) else None
    error = body.get("error") if isinstance(body, dict) else None

    if query_status == "succeeded":
        await _succeed_step(session, step_run)
    elif query_status == "failed":
        await _fail_step(
            session,
            run,
            step_run,
            _clipped(str(error)) if error else "the SQL failed without a reason from loom-query",
        )
    elif query_status == "cancelled":
        # Ai đó gọi `DELETE /query/{id}` lên một query của một pipeline. Không
        # phải thành công, nên chuỗi phải dừng — bước sau chạy trên một bảng chỉ
        # được dựng một nửa là đúng cái hỏng không ai thấy.
        await _fail_step(
            session,
            run,
            step_run,
            f"query {step_run.query_id} was cancelled before it finished, so this step's table "
            "may be half-written. Check it before starting the pipeline again.",
        )
    elif query_status != "running":
        # Một trạng thái `loom-query` chưa từng trả về. ĐỂ NGUYÊN và kêu to:
        # đoán `failed` sẽ giết một bước có thể đang chạy, còn đoán `succeeded`
        # là báo một thành công không ai xác nhận — hướng tệ hơn hẳn.
        logger.error(
            "schedule.query_status_unknown",
            pipeline_run_id=str(run.id),
            step_index=step_run.step_index,
            query_id=step_run.query_id,
            query_status=str(query_status),
        )


async def _start_indexed_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
) -> None:
    """Khởi động `step_run` bằng cấu hình bước ĐỌC TỪ definition của pipeline.

    Cấu hình bước không nằm trong `pipeline_step_run` — hàng đó chỉ giữ trạng
    thái — nên đường đẩy bước phải đọc lại definition. Đó cũng là điều đúng: một
    definition đã bị sửa giữa chừng thì bước sau chạy theo bản MỚI, và không có
    bản chép cũ nào trong bảng để hai thứ lệch nhau.
    """
    item = await _active_item(session, run.pipeline_id, ItemType.pipeline)
    definition = _parsed_pipeline(item) if item is not None else None
    if definition is None or step_run.step_index >= len(definition.steps):
        await _fail_step(
            session,
            run,
            step_run,
            f"step {step_run.step_index} is no longer in this pipeline's definition, "
            "so there is nothing to run — the definition changed while the run was in flight.",
        )
        return

    await _start_step(
        session, app_state, settings, run, step_run, definition.steps[step_run.step_index]
    )


@router.post("/tick")
async def tick(request: Request, session: AsyncSession = SessionDep) -> TickResponse:
    """Xử lý các lịch tới hạn.

    Một tick là một HTTP request — phải trả nhanh. Giới hạn thời gian bằng
    `TICK_BUDGET_SECONDS`; tick tiếp theo xử lý phần còn lại nếu có.
    """
    settings: Settings = request.app.state.settings
    tick_time = datetime.now(UTC)
    return await _process_tick(session, request.app.state, settings, tick_time)
