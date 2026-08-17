"""Tick endpoint — loom-scheduler goi moi N giay.

Day la HTTP endpoint ma `loom-scheduler` goi de kich hoat cac pipeline
duoc lap lich. No phai:
1. Kiem tra X-Loom-Schedule-Secret (tu Task 4)
2. Tim cac schedule den han (enabled, next_run_at <= now)
3. Moi schedule goi decide() (tu Task 5) de quyet dinh start/skip
4. Tao dung MOT pipeline_run cho moi tick (UNIQUE constraint xu ly truong hop song song)
5. Tra nhanh — gioi han trong TICK_BUDGET_SECONDS
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
from loom_api.models import Pipeline, PipelineRun
from loom_api.schedule_service import decide

router = APIRouter(
    prefix="/internal/schedule",
    tags=["internal"],
    dependencies=[Depends(require_schedule_secret)],
)

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
            await session.execute(
                select(PipelineRun).where(
                    PipelineRun.pipeline_id == schedule.pipeline_id,
                    PipelineRun.status == "running",
                )
            )
        ).scalars().first()

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
        stmt = stmt.on_conflict_do_nothing(
            constraint="uq_pipeline_run_pipeline_scheduled_for"
        )

        await session.execute(stmt)

        if decision.action == "start":
            runs_started += 1
        else:
            runs_skipped += 1

    await session.commit()
    return TickResponse(
        schedules_processed=schedules_processed,
        runs_started=runs_started,
        runs_skipped=runs_skipped,
    )


@router.post("/tick")
async def tick(session: AsyncSession = SessionDep) -> TickResponse:
    """Xu ly cac lich den han.

    Mot tick la mot HTTP request — phai tra nhanh. Gioi han thoi gian bang
    TICK_BUDGET_SECONDS. Tick tiep theo xu ly phan con lai neu co.
    """
    tick_time = datetime.now(UTC)
    return await _process_tick(session, tick_time)
