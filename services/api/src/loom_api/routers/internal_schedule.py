"""Tick endpoint — `loom-scheduler` gọi mỗi N giây.

Đây là HTTP endpoint mà `loom-scheduler` gọi để kích hoạt các pipeline được lập
lịch. Nó phải:

1. Kiểm `X-Loom-Schedule-Secret` (Task 4).
2. Tìm các lịch tới hạn — ĐỌC TỪ `item.definition`, xem "Lịch nằm ở đâu" dưới.
3. Mỗi lịch gọi `decide()` (Task 5) để quyết định start/skip.
4. Kiểm LẠI quyền của `run_as_user_id` (Task 8) — xem "Quyền" dưới.
5. Tạo đúng MỘT `pipeline_run` cho mỗi nhịp (ràng buộc UNIQUE lo phần song song).
6. Đẩy các run đang chạy sang bước tiếp theo (Task 7).
7. Trả nhanh — giới hạn trong `TICK_BUDGET_SECONDS`.

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
from loom_api.routers.ingest import launch_ingest_job
from loom_api.schedule_service import decide
from loom_core.config import Settings
from loom_core.cron import CronInvalid, TimezoneInvalid, next_tick
from loom_core.item_definitions import (
    ConnectionDefinition,
    ItemType,
    PipelineDefinition,
    PipelineStep,
    ScheduleDefinition,
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


async def _start_step(
    session: AsyncSession,
    app_state: State,
    settings: Settings,
    run: PipelineRun,
    step_run: PipelineStepRun,
    step: PipelineStep,
) -> None:
    """Khởi động một bước: hàng Postgres TRƯỚC, Job SAU.

    Thứ tự đó là thứ tự của `start_ingest` và không đảo lại được — lý do đầy đủ
    ở docstring `routers/ingest.py`: hàng là *ý định*, Job chỉ là cách ý định
    thành sự thật, và `job_name` tất định theo `run_id` làm việc phóng lại vô
    hại. Đảo lại thì một commit hỏng để lại một pod đang chạy đi hỏi spec của
    một run không tồn tại.
    """
    now = datetime.now(UTC)

    if step.type == "sql":
        # KHOẢNG TRỐNG ĐÃ BIẾT, ghi tên chứ không giấu: chạy câu SQL của một
        # bước cần gọi `loom-query`, và đường đó chưa được nối (Task 11). Bước
        # được đánh `running` và nằm đó — chuỗi KHÔNG tự đi tiếp. Nó không phải
        # một lời nói dối trong bảng ("running" đúng là "đã bắt đầu, chưa xong")
        # nhưng nó cũng chưa phải một bước chạy được.
        step_run.status = "running"
        step_run.started_at = now
        run.status = "running"
        await session.commit()
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
) -> str:
    """Xử lý MỘT nhịp tới hạn. Trả `"started"` / `"skipped"` / `"failed"` / `"raced"`."""
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
        return "raced"

    if status == "failed":
        logger.warning(
            "schedule.run_as_lost_permission",
            pipeline_id=str(item.id),
            run_as_user_id=str(schedule.run_as_user_id),
            scheduled_for=due_at.isoformat(),
        )
        await session.commit()
        return "failed"

    if status == "skipped":
        await session.commit()
        return "skipped"

    run = (
        await session.execute(select(PipelineRun).where(PipelineRun.id == created_id))
    ).scalar_one()
    await _start_run(session, app_state, settings, run, definition)
    # `_start_run` có thể đã đánh hỏng run ngay tại bước 0 (connection biến mất,
    # `secret_ref` không dùng được ở cụm này). Đọc lại trạng thái THẬT thay vì
    # báo "started" cho một run đã `failed` — con số trả về là thứ người vận
    # hành nhìn để biết đêm qua có gì chạy.
    return "failed" if run.status == "failed" else "started"


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
        outcome = await _process_due_pipeline(
            session, app_state, settings, item, definition, schedule, due_at
        )
        counts[outcome] += 1

    await _advance_all_running_runs(session, app_state, settings)

    await session.commit()
    return TickResponse(
        schedules_processed=schedules_processed,
        runs_started=counts["started"],
        runs_skipped=counts["skipped"],
        runs_failed=counts["failed"],
    )


# ------------------------------------------------------------- đẩy run đang chạy


async def _advance_all_running_runs(
    session: AsyncSession, app_state: State, settings: Settings
) -> None:
    """Đẩy tất cả pipeline run đang chạy sang bước tiếp theo."""
    running_runs = (
        (await session.execute(select(PipelineRun).where(PipelineRun.status == "running")))
        .scalars()
        .all()
    )

    for run in running_runs:
        await _advance_run(session, app_state, settings, run)


async def _advance_run(
    session: AsyncSession, app_state: State, settings: Settings, run: PipelineRun
) -> None:
    """Đẩy một `pipeline_run` sang bước tiếp theo.

    Gọi sau khi tick đã xử lý các lịch tới hạn. Chỉ đẩy khi:
    - Run đang ở trạng thái `running`
    - Bước hiện tại đã hoàn thành (`succeeded` hoặc `failed`)
    - Còn bước tiếp theo
    """
    # Tìm bước hiện tại (step_index cao nhất đã succeeded/failed)
    current_step_result = await session.execute(
        select(PipelineStepRun)
        .where(
            PipelineStepRun.pipeline_run_id == run.id,
            PipelineStepRun.status.in_(["succeeded", "failed"]),
        )
        .order_by(PipelineStepRun.step_index.desc())
        .limit(1)
    )
    current_step = current_step_result.scalars().first()

    if current_step is None:
        # Chưa bước nào hoàn thành — chỉ còn một việc đúng: khởi động bước 0 nếu
        # nó vẫn `pending`. Mọi hình dạng khác (bước 0 đang `running`) là một
        # bước đang làm việc, và giục nó là phóng Job thứ hai cho cùng một bước.
        first_pending = (
            (
                await session.execute(
                    select(PipelineStepRun)
                    .where(
                        PipelineStepRun.pipeline_run_id == run.id,
                        PipelineStepRun.status == "pending",
                    )
                    .order_by(PipelineStepRun.step_index)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

        if first_pending is not None and first_pending.step_index == 0:
            await _start_indexed_step(session, app_state, settings, run, first_pending)
        return

    if current_step.status == "failed":
        # Dừng chuỗi — tất cả bước còn lại giữ `pending`. Bước sau chạy trên
        # bronze thiếu dữ liệu sẽ cho ra một bảng silver SAI mà không lỗi nào
        # báo ra, đúng dạng hỏng không ai phát hiện.
        run.status = "failed"
        await session.commit()
        return

    next_step = (
        (
            await session.execute(
                select(PipelineStepRun).where(
                    PipelineStepRun.pipeline_run_id == run.id,
                    PipelineStepRun.step_index == current_step.step_index + 1,
                )
            )
        )
        .scalars()
        .first()
    )

    if next_step is None:
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        return

    await _start_indexed_step(session, app_state, settings, run, next_step)


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
