"""`QueryStore` — dict trong bộ nhớ tiến trình, xem docstring `store.py`."""

from __future__ import annotations

import uuid

from loom_query.schemas import ColumnOut
from loom_query.store import QueryStatus, QueryStore


async def test_unknown_query_id_reads_as_none() -> None:
    store = QueryStore()
    assert await store.get(uuid.uuid4()) is None


async def test_create_then_get_starts_running() -> None:
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.running
    assert state.columns is None
    assert state.rows is None


async def test_set_succeeded_fills_columns_and_rows() -> None:
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)

    await store.set_succeeded(query_id, [ColumnOut(name="a", type="int64")], [[1], [2]])

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.succeeded
    assert state.rows == [[1], [2]]


async def test_cancel_unknown_query_id_returns_false() -> None:
    store = QueryStore()
    assert await store.cancel(uuid.uuid4()) is False


async def test_cancel_a_running_query_marks_it_cancelled() -> None:
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)

    assert await store.cancel(query_id) is True

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.cancelled


async def test_a_result_arriving_after_cancel_is_dropped_not_applied() -> None:
    """Xem docstring `QueryStore.cancel`: một query bị huỷ rồi "xong" sau đó
    không được lặng lẽ quay lại thành `succeeded` — người dùng đã bỏ nó."""
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)
    await store.cancel(query_id)

    await store.set_succeeded(query_id, [ColumnOut(name="a", type="int64")], [[1]])

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.cancelled
    assert state.rows is None


async def test_cancelling_an_already_finished_query_is_a_harmless_no_op() -> None:
    store = QueryStore()
    query_id = uuid.uuid4()
    await store.create(query_id)
    await store.set_succeeded(query_id, [ColumnOut(name="a", type="int64")], [[1]])

    assert await store.cancel(query_id) is True

    state = await store.get(query_id)
    assert state is not None
    assert state.status == QueryStatus.succeeded
    assert state.rows == [[1]]
