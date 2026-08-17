"""Integration tests for POST /internal/schedule/tick."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from loom_api.models import (
    DEFAULT_TENANT_ID,
    Item,
    Pipeline,
    PipelineRun,
)

pytestmark = pytest.mark.integration


async def test_tick_requires_the_shared_secret(api_world) -> None:
    """Khong co header secret -> 401."""
    response = await api_world.client.post("/internal/schedule/tick")
    assert response.status_code == 401


async def test_a_due_schedule_creates_exactly_one_run(api_world) -> None:
    """Mot lich den han -> mot pipeline_run duoc tao."""
    # Setup: create a workspace, user, and pipeline with schedule due now
    session = api_world.app.state.db.session_factory()

    # Create a pipeline item
    pipeline_id = uuid.uuid4()
    session.add(
        Item(
            id=pipeline_id,
            tenant_id=DEFAULT_TENANT_ID,
            workspace_id=api_world.ws_a,
            type="pipeline",
            name="test-pipeline",
            display_name="Test Pipeline",
            definition={
                "schema_version": 1,
                "steps": [],
                "cron": "0 * * * *",
            },
            definition_hash="x" * 64,
            created_by=api_world.user_id,
            updated_by=api_world.user_id,
        )
    )
    await session.flush()

    # Create a Pipeline schedule row due now
    due_at = datetime.now(UTC).replace(second=0, microsecond=0)
    session.add(
        Pipeline(
            id=uuid.uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            cron="0 * * * *",
            enabled=True,
            next_run_at=due_at,
            concurrency_cap=3,
            created_by=api_world.user_id,
            updated_by=api_world.user_id,
        )
    )
    await session.commit()

    # Send tick
    response = await api_world.client.post(
        "/internal/schedule/tick",
        headers={"X-Loom-Schedule-Secret": "dev-only-do-not-use-in-production"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["schedules_processed"] >= 1
    assert data["runs_started"] >= 1

    # Verify ONE pipeline_run was created for this pipeline + tick moment
    runs = (
        await session.execute(
            select(PipelineRun).where(
                PipelineRun.pipeline_id == pipeline_id,
                PipelineRun.scheduled_for == due_at,
            )
        )
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "pending"

    # Cleanup
    await session.execute(delete(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id))
    await session.execute(delete(Pipeline).where(Pipeline.pipeline_id == pipeline_id))
    await session.execute(delete(Item).where(Item.id == pipeline_id))
    await session.commit()


async def test_two_concurrent_ticks_create_exactly_one_run(api_world) -> None:
    """Rang buoc UNIQUE thay the advisory lock — hai tick song song chi tao 1 run."""
    session = api_world.app.state.db.session_factory()

    # Create a pipeline item
    pipeline_id = uuid.uuid4()
    session.add(
        Item(
            id=pipeline_id,
            tenant_id=DEFAULT_TENANT_ID,
            workspace_id=api_world.ws_a,
            type="pipeline",
            name="test-pipeline-concurrent",
            display_name="Test Pipeline Concurrent",
            definition={"schema_version": 1, "steps": [], "cron": "0 * * * *"},
            definition_hash="x" * 64,
            created_by=api_world.user_id,
            updated_by=api_world.user_id,
        )
    )
    await session.flush()

    # Create a schedule due now
    due_at = datetime.now(UTC).replace(second=0, microsecond=0)
    session.add(
        Pipeline(
            id=uuid.uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            cron="0 * * * *",
            enabled=True,
            next_run_at=due_at,
            concurrency_cap=3,
            created_by=api_world.user_id,
            updated_by=api_world.user_id,
        )
    )
    await session.commit()

    tick_headers = {"X-Loom-Schedule-Secret": "dev-only-do-not-use-in-production"}

    # Fire two concurrent ticks
    results = await asyncio.gather(
        api_world.client.post("/internal/schedule/tick", headers=tick_headers),
        api_world.client.post("/internal/schedule/tick", headers=tick_headers),
    )

    # Both should succeed (no 500 from constraint violation)
    assert all(r.status_code == 200 for r in results)

    # Only ONE run should exist despite two concurrent ticks
    runs = (
        await session.execute(
            select(func.count(PipelineRun.id)).where(
                PipelineRun.pipeline_id == pipeline_id,
                PipelineRun.scheduled_for == due_at,
            )
        )
    ).scalar()
    assert runs == 1

    # Cleanup
    await session.execute(delete(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id))
    await session.execute(delete(Pipeline).where(Pipeline.pipeline_id == pipeline_id))
    await session.execute(delete(Item).where(Item.id == pipeline_id))
    await session.commit()


async def test_a_pipeline_still_running_records_a_skipped_row(api_world) -> None:
    """Mot run dang chay -> tick ghi hang skipped voi ly do."""
    session = api_world.app.state.db.session_factory()

    # Create a pipeline item
    pipeline_id = uuid.uuid4()
    session.add(
        Item(
            id=pipeline_id,
            tenant_id=DEFAULT_TENANT_ID,
            workspace_id=api_world.ws_a,
            type="pipeline",
            name="test-pipeline-running",
            display_name="Test Pipeline Running",
            definition={"schema_version": 1, "steps": [], "cron": "0 * * * *"},
            definition_hash="x" * 64,
            created_by=api_world.user_id,
            updated_by=api_world.user_id,
        )
    )
    await session.flush()

    # Create a schedule due now
    due_at = datetime.now(UTC).replace(second=0, microsecond=0)
    session.add(
        Pipeline(
            id=uuid.uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            cron="0 * * * *",
            enabled=True,
            next_run_at=due_at,
            concurrency_cap=3,
            created_by=api_world.user_id,
            updated_by=api_world.user_id,
        )
    )
    await session.flush()

    # Create an ACTIVE (running) pipeline_run for this pipeline
    session.add(
        PipelineRun(
            id=uuid.uuid4(),
            pipeline_id=pipeline_id,
            workspace_id=api_world.ws_a,
            scheduled_for=due_at - timedelta(hours=1),
            status="running",
            run_as_user_id=api_world.user_id,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    await session.commit()

    # Send tick
    response = await api_world.client.post(
        "/internal/schedule/tick",
        headers={"X-Loom-Schedule-Secret": "dev-only-do-not-use-in-production"},
    )
    assert response.status_code == 200
    data = response.json()
    # Should show skipped, not started
    assert data["runs_skipped"] >= 1

    # The skipped row should have a reason
    skip_runs = (
        await session.execute(
            select(PipelineRun).where(
                PipelineRun.pipeline_id == pipeline_id,
                PipelineRun.scheduled_for == due_at,
                PipelineRun.status == "skipped",
            )
        )
    ).scalars().all()
    assert len(skip_runs) == 1
    assert skip_runs[0].skip_reason is not None

    # Cleanup
    await session.execute(delete(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id))
    await session.execute(delete(Pipeline).where(Pipeline.pipeline_id == pipeline_id))
    await session.execute(delete(Item).where(Item.id == pipeline_id))
    await session.commit()
