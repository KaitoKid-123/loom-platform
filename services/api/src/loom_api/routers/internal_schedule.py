"""Tick endpoint — loom-scheduler goi moi N giay.

Day la HTTP endpoint ma `loom-scheduler` goi de kich hoat cac pipeline
duoc lap lich. No phai:
1. Kiem tra X-Loom-Schedule-Secret (tu Task 4)
2. Tim cac schedule den han (enabled, next_run_at <= now)
3. Moi schedule goi decide() (tu Task 5) de quyet dinh start/skip
4. Tao dung MOT pipeline_run cho moi tick (UNIQUE constraint xu ly truong hop song song)
5. Day cac pending/running runs sang buoc tiep theo (Task 7)
6. Tra nhanh — gioi han trong TICK_BUDGET_SECONDS
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import SessionDep
from loom_api.internal_security import require_schedule_secret
from loom_api.models import Pipeline, PipelineRun, PipelineStepRun
from loom_api.schedule_service import decide

router = APIRouter(tags=["internal"], dependencies=[Depends(require_schedule_secret)])

TICK_BUDGET_SECONDS = 20


class TickResponse(BaseModel):
    schedules_processed: int
    runs_started: int
    runs_skipped: int


async def _process_tick(session: AsyncSession, tick_time: datetime) -> TickResponse:
    """Tim cac schedule den han, quyet dinh start/skip, tao pipeline_run rows."""
    # Tim cac schedule den han: enabled va next_run_at <= tick_time
    due_schedules = (
        (
            await session.execute(
                select(Pipeline).where(
                    Pipeline.enabled == True,  # noqa: E712
                    Pipeline.next_run_at <= tick_time,
                )
            )
        )
        .scalars()
        .all()
    )

    schedules_processed = 0
    runs_started = 0
    runs_skipped = 0

    for schedule in due_schedules:
        schedules_processed += 1

        # Dem so run dang chay cua pipeline nay
        active_runs_count_result = await session.execute(
            select(func.count(PipelineRun.id)).where(
                PipelineRun.pipeline_id == schedule.pipeline_id,
                PipelineRun.status == "running",
            )
        )
        active_runs_count = active_runs_count_result.scalar() or 0

        # Kiem tra co run nao dang chay khong
        active_run = (
            (
                await session.execute(
                    select(PipelineRun).where(
                        PipelineRun.pipeline_id == schedule.pipeline_id,
                        PipelineRun.status == "running",
                    )
                )
            )
            .scalars()
            .first()
        )

        # Quyet dinh start hay skip
        decision = decide(
            due_at=tick_time,
            has_active_run=active_run is not None,
            active_run_started_at=active_run.started_at if active_run else None,
            concurrent_runs=active_runs_count,
            concurrency_cap=schedule.concurrency_cap,
        )

        # Tao pipeline_run row — dung INSERT ON CONFLICT de xu ly truong hop song song
        # UNIQUE constraint tren (pipeline_id, scheduled_for) dam bao chi mot run
        # duoc tao cho moi tick cua moi pipeline
        run_id = uuid.uuid4()
        stmt = pg_insert(PipelineRun).values(
            id=run_id,
            pipeline_id=schedule.pipeline_id,
            workspace_id=schedule.workspace_id,
            scheduled_for=schedule.next_run_at,
            status="skipped" if decision.action == "skip" else "pending",
            skip_reason=decision.reason if decision.action == "skip" else None,
            run_as_user_id=schedule.created_by,
        )

        # ON CONFLICT DO NOTHING — neu tick khac da tao roi thi bo qua
        stmt = stmt.on_conflict_do_nothing(constraint="uq_pipeline_run_pipeline_scheduled_for")

        await session.execute(stmt)

        if decision.action == "start":
            runs_started += 1
        else:
            runs_skipped += 1

    # Day cac running runs sang buoc tiep theo
    await _advance_all_running_runs(session)

    await session.commit()
    return TickResponse(
        schedules_processed=schedules_processed,
        runs_started=runs_started,
        runs_skipped=runs_skipped,
    )


async def _advance_all_running_runs(session: AsyncSession) -> None:
    """Day tat ca running pipeline runs sang buoc tiep theo."""
    running_runs = (
        (await session.execute(select(PipelineRun).where(PipelineRun.status == "running")))
        .scalars()
        .all()
    )

    for run in running_runs:
        await _advance_run(session, run)


async def _advance_run(session: AsyncSession, run: PipelineRun) -> None:
    """Day mot pipeline_run sang buoc tiep theo.

    Goi sau khi tick da xu ly cac lich den han.
    Chi day khi:
    - Run dang o trang thai "running"
    - Buoc hien tai da hoan thanh (success hoac failed)
    - Con buoc tiep theo
    """
    # Tim buoc hien tai (step_index thap nhat chua succeeded/failed)
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
        # Chua co buoc nao hoan thanh — kiem tra co buoc pending chua?
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
            # Bat dau tu buoc 0
            first_pending.status = "running"
            first_pending.started_at = datetime.now(UTC)
            await session.commit()
        return

    if current_step.status == "failed":
        # Dung chuoi — tat ca buoc con lai giu pending
        run.status = "failed"
        await session.commit()
        return

    # Buoc hien tai da thanh cong — tim buoc tiep theo
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
        # Khong con buoc — run hoan thanh
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        return

    # Khoi dong buoc tiep theo
    next_step.status = "running"
    next_step.started_at = datetime.now(UTC)
    # TODO: Goi start_ingest() hoac start_sql() tuy loai step
    await session.commit()


@router.post("/tick")
async def tick(session: AsyncSession = SessionDep) -> TickResponse:
    """Xu ly cac lich den han.

    Mot tick la mot HTTP request — phai tra nhanh. Gioi han thoi gian bang
    TICK_BUDGET_SECONDS. Tick tiep theo xu ly phan con lai neu co.
    """
    tick_time = datetime.now(UTC)
    return await _process_tick(session, tick_time)
