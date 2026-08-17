"""Logic thuần quyết định có chạy một tick hay bỏ nhịp."""

from datetime import UTC, datetime

from loom_api.schedule_service import decide


def test_a_pipeline_already_running_is_skipped_with_a_readable_reason() -> None:
    decision = decide(
        due_at=datetime(2026, 8, 15, 2, 0, tzinfo=UTC),
        has_active_run=True,
        active_run_started_at=datetime(2026, 8, 14, 2, 0, tzinfo=UTC),
        concurrent_runs=0,
        concurrency_cap=3,
    )
    assert decision.action == "skip"
    assert "2026-08-14" in decision.reason, "lý do phải nói run nào đang chặn"


def test_the_global_cap_skips_rather_than_queues() -> None:
    decision = decide(
        due_at=datetime(2026, 8, 15, 2, 0, tzinfo=UTC),
        has_active_run=False,
        active_run_started_at=None,
        concurrent_runs=3,
        concurrency_cap=3,
    )
    assert decision.action == "skip"
    assert "3/3" in decision.reason


def test_a_clear_slot_starts() -> None:
    decision = decide(
        due_at=datetime(2026, 8, 15, 2, 0, tzinfo=UTC),
        has_active_run=False,
        active_run_started_at=None,
        concurrent_runs=1,
        concurrency_cap=3,
    )
    assert decision.action == "start"
