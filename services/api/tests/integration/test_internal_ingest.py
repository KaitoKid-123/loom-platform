"""`/internal/ingest/*` — ba đường mà pod nạp gọi ngược về control plane.

Ở `integration/` chứ không ở `services/api/tests/`: cả ba route ĐỌC VÀ GHI
Postgres (`ingest_run`, `stream_state`), và `conftest.py` của thư mục kia chỉ
dựng một app — không có database nào. Một bộ test chỉ dựng app kiểm được đúng
cổng 401 và không kiểm được điều quan trọng nhất ở đây: watermark thật sự nằm ở
đâu sau ba lần báo tiến độ.

Chạy qua HTTP THẬT trên `api_world`, cùng lý do `test_ingest_api.py` ghi: một
luật đúng ở tầng dưới vẫn có thể bị bỏ qua ở tầng router, và chỉ đường HTTP mới
thấy điều đó. KHÁC `test_ingest_api.py` ở một chỗ: những đường này không có
principal nào và `api_world.grant(...)` không liên quan — cổng duy nhất là bí
mật chia sẻ trong header.

Hàng `ingest_run` ở đây chèn THẲNG chứ không đi qua `POST /api/v1/lakehouses/
{id}/ingest`: đường đó phóng một Job k8s (cần một double), và nó đã có bộ test
riêng. Điều đang kiểm ở file này bắt đầu TỪ một run đã tồn tại.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, IngestRun, Item, StreamState
from loom_api.routers.internal_ingest import _advance_watermark
from loom_core.internal_auth import INGEST_SHARED_SECRET_HEADER
from loom_core.item_definitions import ItemType

from .conftest import ApiWorld

pytestmark = pytest.mark.integration

# Trong `secret_ref` — nó là tên KHOÁ bên trong k8s Secret, không phải mật khẩu
# (xem `SECRET_REF_RE`). Dẫn câu khẳng định "spec không mang mật khẩu" từ chính
# hằng số này là cách duy nhất để nó không thành no-op khi ai đó đổi giá trị.
REF_KEY = "pg-app-password"
K8S_REF = f"k8s://loom/source-pg#{REF_KEY}"

SOURCE_HOST = "db.example.internal"
SOURCE_DATABASE = "sales"
STREAM = "public.orders"


def _maker(world: ApiWorld) -> async_sessionmaker[Any]:
    return async_sessionmaker(world.engine, expire_on_commit=False)


def _headers(world: ApiWorld) -> dict[str, str]:
    """Bí mật đọc TỪ `Settings` của chính app đang chạy, không viết cứng.

    Viết cứng `"dev-only-do-not-use-in-production"` sẽ làm mọi test dưới đây đỏ
    hàng loạt vào ngày mặc định đổi — với một thông báo 401 chẳng nói gì về
    nguyên nhân. Dẫn từ `app.state.settings` thì phép kiểm nói đúng điều nó
    muốn nói: header khớp CẤU HÌNH của server thì qua cổng.
    """
    return {INGEST_SHARED_SECRET_HEADER: world.app.state.settings.ingest_shared_secret}


async def _insert_item(
    world: ApiWorld, item_type: ItemType, definition: dict[str, Any]
) -> uuid.UUID:
    item_id = uuid.uuid4()
    async with _maker(world)() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=world.ws_a,
                type=str(item_type),
                name=f"{item_type}-{item_id.hex[:8]}",
                display_name=str(item_type),
                definition=definition,
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


async def _run(world: ApiWorld, *, mode: str = "incremental", status: str = "pending") -> IngestRun:
    """Một hàng `ingest_run` đã COMMIT, kèm lakehouse và connection của nó."""
    lakehouse_id = await _insert_item(world, ItemType.lakehouse, {"schema_version": 1})
    connection_id = await _insert_item(
        world,
        ItemType.connection,
        {
            "schema_version": 1,
            "kind": "postgres",
            "host": SOURCE_HOST,
            "port": 5432,
            "database": SOURCE_DATABASE,
            "secret_ref": K8S_REF,
        },
    )
    run = IngestRun(
        id=uuid.uuid4(),
        lakehouse_id=lakehouse_id,
        connection_id=connection_id,
        workspace_id=world.ws_a,
        stream=STREAM,
        mode=mode,
        status=status,
    )
    async with _maker(world)() as session:
        session.add(run)
        await session.commit()
    return run


async def _sibling_run(
    world: ApiWorld, run: IngestRun, *, mode: str = "incremental", status: str = "pending"
) -> IngestRun:
    """Một run THỨ HAI trên CÙNG (lakehouse, connection, stream) — cấu hình mà
    `start_ingest` cho phép tồn tại vì nó KHÔNG có cổng chống trùng, và
    `job_name` tất định theo `run_id` chứ không theo stream (xem `jobs.py`), nên
    hai lần bấm Nạp cho ra hai Job cùng ghi một stream."""
    sibling = IngestRun(
        id=uuid.uuid4(),
        lakehouse_id=run.lakehouse_id,
        connection_id=run.connection_id,
        workspace_id=world.ws_a,
        stream=run.stream,
        mode=mode,
        status=status,
    )
    async with _maker(world)() as session:
        session.add(sibling)
        await session.commit()
    return sibling


async def _run_row(world: ApiWorld, run_id: uuid.UUID) -> IngestRun:
    async with _maker(world)() as session:
        return (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()


async def _item_name(world: ApiWorld, item_id: uuid.UUID) -> str:
    """Cột `item.name` — nguồn sự thật của `IngestSpec.connection_slug`.

    Đọc lại từ database chứ không dựng lại chuỗi mà `_insert_item` đã sinh: nếu
    quy ước đặt tên trong fixture đổi, phép khẳng định vẫn nói đúng điều nó muốn
    nói ("slug đến TỪ cột name"), không thành một phép so hai chuỗi trùng nhau vì
    cùng được viết ra hai lần.
    """
    async with _maker(world)() as session:
        return (await session.execute(select(Item.name).where(Item.id == item_id))).scalar_one()


async def _watermark(world: ApiWorld, run: IngestRun) -> StreamState | None:
    async with _maker(world)() as session:
        return (
            await session.execute(
                select(StreamState).where(
                    StreamState.lakehouse_id == run.lakehouse_id,
                    StreamState.connection_id == run.connection_id,
                    StreamState.stream == run.stream,
                )
            )
        ).scalar_one_or_none()


async def _assert_parked_in_postgres(task: asyncio.Task[None]) -> None:
    """Khẳng định `task` đang bị Postgres KHOÁ — không phải chỉ "chưa được lập lịch".

    Phiên bản đầu của phép kiểm này dùng `for _ in range(50): await
    asyncio.sleep(0)` rồi `assert not task.done()`. Nó SAI, và đã tự chứng minh
    là sai: dưới bản cài đặt `SELECT`-rồi-`INSERT` (không an toàn với đua), câu
    khẳng định đó vẫn XANH khi chạy cả file, vì `sleep(0)` chỉ nhường vòng lặp
    và không cho asyncpg đủ lượt để hoàn thành một round trip — "chưa xong" khi
    đó chỉ có nghĩa "chưa kịp bắt đầu". Đúng kiểu xanh-nhờ-may-mắn.

    `asyncio.wait({task}, timeout=...)` chờ THỜI GIAN THẬT và KHÔNG huỷ task
    (khác `wait_for`, thứ huỷ khi hết hạn). Sau hai giây thật, một câu SELECT
    hay INSERT trên container Postgres nội bộ đã phải xong từ lâu — nên "vẫn
    còn pending" chỉ còn một cách giải thích: nó đang chờ một khoá.

    Sai theo hướng nào cũng ĐỎ, không bao giờ xanh oan: nếu máy tải nặng tới
    mức một round trip mất hơn hai giây, câu này vẫn đúng nhưng vì lý do sai —
    nên phép chứng minh đỏ (đổi `_advance_watermark` về `SELECT`-rồi-`INSERT`)
    là thứ xác nhận nó thật sự canh đúng cửa sổ, và nó đã được chạy.
    """
    done, _pending = await asyncio.wait({task}, timeout=2.0)
    if done:
        # `task.exception()` để một ngoại lệ bên trong hiện ra thay vì bị nuốt
        # sau lưng một `AssertionError` chung chung.
        outcome = task.exception() if not task.cancelled() else "bị huỷ"
        raise AssertionError(
            "`second` chạy xong dù `first` chưa commit, nên cửa sổ đua KHÔNG được "
            "đi vào và phép kiểm này không chứng minh điều nó nêu tên. "
            f"Kết quả của task: {outcome!r}. `_advance_watermark` phải hiện-thực-hoá "
            "hàng (INSERT ... ON CONFLICT DO NOTHING) rồi khoá nó (SELECT ... FOR "
            "UPDATE), chứ không SELECT rồi mới INSERT."
        )


def _progress(cursor_value: str, cursor_type: str = "bigint", **extra: Any) -> dict[str, Any]:
    return {
        "rows": 100,
        "cursor_column": "id",
        "cursor_type": cursor_type,
        "cursor_value": cursor_value,
        **extra,
    }


# --------------------------------------------------------------------- cổng bí mật


async def test_the_spec_requires_the_shared_secret(api_world: ApiWorld) -> None:
    run = await _run(api_world)
    response = await api_world.client.get(f"/internal/ingest/{run.id}/spec")
    assert response.status_code == 401, response.text


async def test_progress_and_complete_are_gated_by_the_same_dependency(
    api_world: ApiWorld,
) -> None:
    """Cả BA route đứng sau MỘT `dependencies=` ở cấp `APIRouter`, không phải
    ba `Depends()` dán tay mà một cái có thể thiếu.

    Thân request cố ý HỢP LỆ: nếu cổng bí mật lỡ chạy SAU phần validate thân
    request, một thân hợp lệ vẫn phải 401 — cách duy nhất phân biệt thứ tự đó
    từ bên ngoài là không cho thân request là lý do bị từ chối.
    """
    run = await _run(api_world)
    progress = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress", json=_progress("400")
    )
    complete = await api_world.client.post(
        f"/internal/ingest/{run.id}/complete", json={"status": "succeeded"}
    )
    assert (progress.status_code, complete.status_code) == (401, 401)


async def test_a_wrong_secret_is_401_not_403_or_500(api_world: ApiWorld) -> None:
    """403 ngụ ý "đã biết bạn là ai, chỉ là không đủ quyền" — sai hoàn toàn về
    BẢN CHẤT ở đây, chưa có danh tính nào được xác thực cả. 500 nghĩa là phép so
    ném ngoại lệ (ví dụ `compare_digest` gặp giá trị không phải ASCII)."""
    run = await _run(api_world)
    response = await api_world.client.get(
        f"/internal/ingest/{run.id}/spec",
        headers={INGEST_SHARED_SECRET_HEADER: "khong-phai-bi-mat-that"},
    )
    assert response.status_code == 401, response.text


async def test_a_header_with_non_ascii_bytes_is_401_not_500(api_world: ApiWorld) -> None:
    """Lỗi THẬT, đã dựng lại trước khi sửa — không phải một khả năng lý thuyết.

    `hmac.compare_digest` trên hai `str` ném `TypeError: comparing strings with
    non-ASCII characters is not supported`, và Starlette giải mã header bằng
    latin-1, nên đúng MỘT byte >127 là đủ. Trước khi đổi sang so trên bytes,
    request dưới đây trả 500 kèm nguyên traceback trong log — một request KHÔNG
    cần xác thực gì mà ép được server 500.

    Header truyền dưới dạng BYTES THÔ, không phải `str`: httpx từ chối mã hoá
    một `str` ngoài ASCII vào header (`UnicodeEncodeError` ở phía client), nên
    một phép kiểm viết bằng `str` sẽ đỏ ở CLIENT và không bao giờ chạm tới
    server — tức là canh nhầm chỗ. `b"\\xc3\\xa9"` là những gì một client bất kỳ
    (curl, một pod tự viết) đặt lên dây được.
    """
    run = await _run(api_world)
    response = await api_world.client.get(
        f"/internal/ingest/{run.id}/spec",
        headers={INGEST_SHARED_SECRET_HEADER.encode(): b"bi-mat\xc3\xa9"},
    )
    assert response.status_code == 401, response.text


async def test_the_gate_compares_the_ingest_secret_not_the_query_secret(
    api_world: ApiWorld,
) -> None:
    """Phép kiểm DUY NHẤT ghim được việc TÁCH hai bí mật, và nó phải ở đây.

    `packages/core/tests/test_config.py::test_the_ingest_secret_is_not_the_query_secret`
    KHÔNG ghim được: nó chỉ khẳng định hai TRƯỜNG `Settings` đọc hai biến môi
    trường khác nhau, không bao giờ chạm tới cái cổng. Đã chứng minh: đổi
    `settings.ingest_shared_secret` -> `settings.query_shared_secret` ở
    `internal_security.py` để lại TOÀN BỘ bộ test xanh, vì trong test cả hai
    trường cùng mang một chuỗi placeholder. Tức là tính chất an ninh mà việc
    tách bí mật tồn tại để tạo ra đúng trong mã và KHÔNG được canh bởi bất cứ
    gì — cách một lần tái cấu trúc là lặng lẽ mất.

    Vá `app.state.settings` bằng `model_copy` chứ không `monkeypatch.setenv`:
    `get_settings()` là `lru_cache` và `create_app()` đã gọi nó xong từ lúc
    `api_world` dựng app, nên đặt biến môi trường ở đây là vô tác dụng.
    `model_copy` trả về một đối tượng MỚI (đã kiểm: `Settings` là `frozen`,
    và bản gốc trong cache không bị sửa), nên nó không rò sang test khác dùng
    cùng `Settings` đã cache.

    Câu khẳng định thứ HAI là câu chịu lực: bí mật của `loom-query` phải bị TỪ
    CHỐI. Chỉ khẳng định bí mật đúng đi qua thì một bản cài đặt so với
    `query_shared_secret` vẫn xanh, vì ở đó hai giá trị bằng nhau.
    """
    ingest_secret = "value-of-the-ingest-secret"
    query_secret = "value-of-the-query-secret"
    api_world.app.state.settings = api_world.app.state.settings.model_copy(
        update={"ingest_shared_secret": ingest_secret, "query_shared_secret": query_secret}
    )
    run = await _run(api_world)
    spec = f"/internal/ingest/{run.id}/spec"

    with_ingest = await api_world.client.get(
        spec, headers={INGEST_SHARED_SECRET_HEADER: ingest_secret}
    )
    with_query = await api_world.client.get(
        spec, headers={INGEST_SHARED_SECRET_HEADER: query_secret}
    )

    # MỘT câu khẳng định trên CẢ HAI mã trạng thái, không phải hai câu nối tiếp:
    # dưới bản cài đặt sai thì cả hai vế đều lệch, và hai `assert` rời nhau chỉ
    # báo được vế nào chạy trước — che mất đúng nửa thông tin cần để chẩn đoán.
    assert (with_ingest.status_code, with_query.status_code) == (200, 401), (
        f"ingest secret -> {with_ingest.status_code} (phải 200), "
        f"query secret -> {with_query.status_code} (phải 401). "
        "Cổng đang so với `settings.query_shared_secret` thay vì "
        "`settings.ingest_shared_secret`: việc tách bí mật (xem "
        "`Settings.task_shared_secret_key`) chỉ còn trên giấy, và một pod nạp bị "
        "chiếm lại giả được loom-api với loom-query dưới danh nghĩa bất kỳ "
        "principal nào."
    )


async def test_the_right_secret_passes_the_gate(api_world: ApiWorld) -> None:
    """Chốt chống-xanh-rỗng cho ba phép kiểm trên: nếu MỌI request đều 401 vì
    một lý do khác hẳn (route gắn nhầm prefix, chẳng hạn), cả ba xanh mà không
    canh gì."""
    run = await _run(api_world)
    response = await api_world.client.get(
        f"/internal/ingest/{run.id}/spec", headers=_headers(api_world)
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- spec


async def test_the_spec_never_returns_the_source_password(api_world: ApiWorld) -> None:
    """Pod nạp lấy mật khẩu từ k8s Secret qua `envFrom`, KHÔNG từ đây.

    Nếu spec trả cả mật khẩu (hoặc cả con trỏ tới nó) thì thiết kế "control
    plane không đọc credential nguồn" sụp — và nó sụp một cách IM LẶNG, vì mọi
    thứ vẫn chạy y nguyên.

    Quét trên TOÀN BỘ thân phản hồi đã serialize, không trên từng trường: một
    trường mới thêm vào `IngestSpec` sau này tự động nằm trong phạm vi quét mà
    không ai phải nhớ thêm một dòng assert.
    """
    run = await _run(api_world)
    body = (
        await api_world.client.get(f"/internal/ingest/{run.id}/spec", headers=_headers(api_world))
    ).json()
    flat = json.dumps(body).lower()
    assert "password" not in flat
    assert "secret" not in flat
    # Dẫn từ hằng số dựng ref, không từ chữ "password" chung chung: câu khẳng
    # định trên thành no-op ngay khi ai đó đổi tên khoá trong fixture.
    assert REF_KEY not in flat
    assert "k8s://" not in flat


async def test_the_spec_says_what_to_ingest_and_where_from(api_world: ApiWorld) -> None:
    run = await _run(api_world, mode="incremental")
    body = (
        await api_world.client.get(f"/internal/ingest/{run.id}/spec", headers=_headers(api_world))
    ).json()
    assert body["run_id"] == str(run.id)
    assert body["lakehouse_id"] == str(run.lakehouse_id)
    assert body["workspace_id"] == str(api_world.ws_a)
    # `connection_id` là thứ pod ghi vào cột bronze `_source` (spec mục 5.5) —
    # nó không có đường nào khác để biết giá trị này, nên nó phải có mặt ở đây.
    assert body["connection_id"] == str(run.connection_id)
    # `connection_slug` là `item.name` của connection, và nó đi vào TÊN BẢNG
    # bronze (`bronze.<slug>__<schema>_<bảng>`, spec mục 5). Pod không đọc được
    # bảng `item` (không có credential Postgres control plane), nên spec là đường
    # DUY NHẤT để cái tên đó tới được nó — thiếu trường này thì `loom-task` chỉ
    # còn `connection_id` để đặt tên bảng, và không ai đọc ngược ra nguồn được từ
    # một uuid. Dẫn từ hàng `item` THẬT chứ không từ một chuỗi viết cứng: câu
    # khẳng định phải nói "slug đến từ cột `name`", không phải "slug tình cờ bằng
    # chuỗi này".
    assert body["connection_slug"] == await _item_name(api_world, run.connection_id)
    assert (body["stream"], body["mode"]) == (STREAM, "incremental")
    assert body["source"] == {
        "kind": "postgres",
        "host": SOURCE_HOST,
        "port": 5432,
        "database": SOURCE_DATABASE,
    }
    # Chưa có lần nạp nào -> chưa có watermark. Cả BA trường cùng `None`, không
    # phải hai trong ba: một `cursor_value` không kèm `cursor_type` là một chuỗi
    # không so sánh được.
    assert (body["cursor_column"], body["cursor_type"], body["cursor_value"]) == (
        None,
        None,
        None,
    )


async def test_fetching_the_spec_moves_a_pending_run_to_running(api_world: ApiWorld) -> None:
    """`pending` có nghĩa CỤ THỂ — "Job chưa bao giờ khởi động được" (xem
    `IngestRun`) — và vòng đối chiếu của Task 13 tồn tại để đánh `failed` đúng
    những hàng đó. Lấy spec là bằng chứng duy nhất control plane có rằng pod đã
    chạy thật; không chuyển ở đây thì một lần quét toàn bảng dài sẽ bị một vòng
    đối chiếu đánh chết oan."""
    run = await _run(api_world, status="pending")
    await api_world.client.get(f"/internal/ingest/{run.id}/spec", headers=_headers(api_world))
    assert (await _run_row(api_world, run.id)).status == "running"


async def test_fetching_the_spec_does_not_revive_a_finished_run(api_world: ApiWorld) -> None:
    """Chỉ `pending` -> `running`. Một lần gọi lại spec sau khi run đã đóng
    (thử lại, gọi nhầm) không được kéo nó ra khỏi trạng thái cuối — nếu được,
    một run đã `failed` sẽ trông như đang chạy mãi mãi."""
    run = await _run(api_world, status="failed")
    await api_world.client.get(f"/internal/ingest/{run.id}/spec", headers=_headers(api_world))
    assert (await _run_row(api_world, run.id)).status == "failed"


async def test_an_incremental_spec_carries_the_stored_watermark(api_world: ApiWorld) -> None:
    run = await _run(api_world, mode="incremental")
    await api_world.client.post(
        f"/internal/ingest/{run.id}/progress", json=_progress("400"), headers=_headers(api_world)
    )
    body = (
        await api_world.client.get(f"/internal/ingest/{run.id}/spec", headers=_headers(api_world))
    ).json()
    assert (body["cursor_column"], body["cursor_type"], body["cursor_value"]) == (
        "id",
        "bigint",
        "400",
    )


async def test_a_full_run_is_not_handed_the_stored_watermark(api_world: ApiWorld) -> None:
    """ "full" nghĩa là đọc lại TỪ ĐẦU. Đưa watermark cho pod ở chế độ đó là mời
    nó lặng lẽ làm một lần nạp gia tăng dưới cái tên "full" — người bấm nút
    "nạp lại toàn bộ" nhận về đúng thứ họ vừa cố tránh, và không lỗi nào báo.

    Hàng `stream_state` vẫn phải NGUYÊN VẸN sau đó: lần nạp incremental kế tiếp
    dùng lại nó.
    """
    run = await _run(api_world, mode="incremental")
    await api_world.client.post(
        f"/internal/ingest/{run.id}/progress", json=_progress("400"), headers=_headers(api_world)
    )
    full = await _sibling_run(api_world, run, mode="full")

    body = (
        await api_world.client.get(f"/internal/ingest/{full.id}/spec", headers=_headers(api_world))
    ).json()
    assert (body["cursor_column"], body["cursor_type"], body["cursor_value"]) == (
        None,
        None,
        None,
    )
    state = await _watermark(api_world, run)
    assert state is not None and state.cursor_value == "400"


# ----------------------------------------------------------------------- watermark


async def test_progress_advances_the_watermark(api_world: ApiWorld) -> None:
    run = await _run(api_world)
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress", json=_progress("400"), headers=_headers(api_world)
    )
    assert response.status_code == 204, response.text
    state = await _watermark(api_world, run)
    assert state is not None
    assert (state.cursor_column, state.cursor_type, state.cursor_value) == ("id", "bigint", "400")


async def test_progress_never_moves_the_watermark_backwards(api_world: ApiWorld) -> None:
    """CẶP GIÁ TRỊ LÀ CỐ Ý.

    `("400", "200")` — cặp mà một bản kế hoạch trước dùng — XANH y nguyên với
    một bản cài đặt so sánh CHUỖI, tức là chứng nhận đúng con bug cần chặn.
    `"1000"` so với `"400"` phân biệt được: so sánh SỐ cho 1000 > 400 (tiến), so
    sánh CHUỖI cho "1000" < "400" (lùi).

    Gửi lần lượt "400", "1000", "400" — phải còn "1000". Ba lô chứ không hai:
    lô cuối là vế "không lùi", còn lô giữa là vế "vẫn tiến được", và một bản cài
    đặt hỏng theo hướng nào cũng chỉ đỏ ở một trong hai.
    """
    run = await _run(api_world)
    for value in ("400", "1000", "400"):
        response = await api_world.client.post(
            f"/internal/ingest/{run.id}/progress",
            json=_progress(value),
            headers=_headers(api_world),
        )
        assert response.status_code == 204, response.text

    state = await _watermark(api_world, run)
    assert state is not None
    assert state.cursor_value == "1000", (
        "Watermark lùi về '400'. Với `cursor_type='bigint'` thì 1000 > 400, "
        "nhưng '1000' < '400' khi so CHUỖI — phép so phải đi qua "
        "`loom_core.cursor.parse_cursor_value`, không so trên `str`."
    )


async def test_the_watermark_only_moves_forward_for_timestamps_too(
    api_world: ApiWorld,
) -> None:
    """Cùng luật, kiểu khác. Timestamp ISO-8601 sắp xếp CHUỖI lại ĐÚNG, nên nó
    là nửa số kiểu mà con bug so-sánh-chuỗi KHÔNG lộ ra — phép kiểm này một
    mình xanh trên đúng bản cài đặt hỏng, nên nó chỉ có nghĩa khi đứng cạnh
    `test_progress_never_moves_the_watermark_backwards`."""
    run = await _run(api_world)
    for value in ("2026-08-12T23:59:59", "2026-08-13T00:00:01", "2026-08-12T23:59:59"):
        response = await api_world.client.post(
            f"/internal/ingest/{run.id}/progress",
            json=_progress(value, cursor_type="timestamp without time zone", rows=1),
            headers=_headers(api_world),
        )
        assert response.status_code == 204, response.text

    state = await _watermark(api_world, run)
    assert state is not None
    assert state.cursor_value == "2026-08-13T00:00:01"


async def test_changing_the_cursor_column_resets_the_watermark(api_world: ApiWorld) -> None:
    """Hai giá trị trên hai THANG ĐO khác nhau — "lớn hơn" giữa chúng không
    mang nghĩa gì.

    `id=91234` với `updated_at=2026-08-13`: so sánh sẽ hoặc khoá chết watermark
    mới ở một mốc cũ khổng lồ, hoặc cho qua một mốc vô nghĩa. Đặt lại là câu trả
    lời đúng, và nó cũng chính là lý do `uq_stream_state_lakehouse_connection_
    stream` CỐ Ý không có `cursor_column` trong khoá (xem `StreamState`).
    """
    run = await _run(api_world)
    await api_world.client.post(
        f"/internal/ingest/{run.id}/progress",
        json=_progress("91234"),
        headers=_headers(api_world),
    )
    await api_world.client.post(
        f"/internal/ingest/{run.id}/progress",
        json={
            "rows": 5,
            "cursor_column": "updated_at",
            "cursor_type": "date",
            "cursor_value": "2026-08-13",
        },
        headers=_headers(api_world),
    )

    state = await _watermark(api_world, run)
    assert state is not None
    # MỘT hàng, đã viết lại — không phải hai hàng để lần nạp sau chọn bừa.
    assert (state.cursor_column, state.cursor_type, state.cursor_value) == (
        "updated_at",
        "date",
        "2026-08-13",
    )


async def test_a_watermark_written_before_this_migration_is_reset_not_compared(
    api_world: ApiWorld,
) -> None:
    """`cursor_type IS NULL` = hàng có từ trước migration 0006, kiểu KHÔNG BIẾT.

    Không có nhánh đặt-lại, `moves_forward` sẽ phải đọc giá trị cũ dưới một kiểu
    do lô MỚI khai — hoặc ném (500 vĩnh viễn cho stream đó), hoặc thành công với
    một nghĩa khác hẳn. Chèn thẳng một hàng như vậy là cách duy nhất dựng lại
    trạng thái đó, vì chính đường báo tiến độ không bao giờ tạo ra nó.

    Giá trị `"99999"` cố ý LỚN HƠN `"400"` theo cả hai cách so: nếu bản cài đặt
    đem ra so thay vì đặt lại thì watermark ở lại `"99999"` và test đỏ.
    """
    run = await _run(api_world)
    async with _maker(api_world)() as session:
        session.add(
            StreamState(
                id=uuid.uuid4(),
                lakehouse_id=run.lakehouse_id,
                connection_id=run.connection_id,
                stream=run.stream,
                cursor_column="id",
                cursor_type=None,
                cursor_value="99999",
            )
        )
        await session.commit()

    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress", json=_progress("400"), headers=_headers(api_world)
    )

    assert response.status_code == 204, response.text
    state = await _watermark(api_world, run)
    assert state is not None
    assert (state.cursor_type, state.cursor_value) == ("bigint", "400")


async def test_two_runs_reporting_one_stream_at_once_never_500(api_world: ApiWorld) -> None:
    """Đường HTTP THẬT, hai lần báo tiến độ ĐẦU TIÊN chạy đồng thời.

    Đây là hình dạng mà một người dùng tạo ra bằng cách bấm Nạp hai lần:
    `start_ingest` không chống trùng, nên hai `ingest_run` cùng sống và hai Job
    cùng ghi một stream. Trước khi `_advance_watermark` chuyển sang
    hiện-thực-hoá-rồi-khoá, cả hai đều thấy `None` rồi cùng `INSERT`, và một
    cái vỡ ở `uq_stream_state_lakehouse_connection_stream` -> `IntegrityError`
    -> 500.

    **Phép kiểm này KHÔNG bảo đảm cửa sổ đua bị đi vào** — nó phụ thuộc hai
    request đan nhau đúng chỗ, và `asyncio.gather` không hứa điều đó. Nó ở đây
    để chứng minh phần NỐI DÂY (router -> handler -> hàm) đúng đầu-cuối. Phép
    kiểm ngay dưới mới là cái ép cửa sổ đó mở ra một cách xác định.

    `pool_size=2, max_overflow=0` ở `api_world` (xem `conftest.py`) là vừa đủ
    cho hai request đồng thời; nhiều hơn hai sẽ chặn ở pool chứ không ở
    Postgres, và khi đó phép kiểm đo hàng đợi của SQLAlchemy thay vì đo khoá.
    """
    run_a = await _run(api_world)
    run_b = await _sibling_run(api_world, run_a)
    headers = _headers(api_world)

    first, second = await asyncio.gather(
        api_world.client.post(
            f"/internal/ingest/{run_a.id}/progress", json=_progress("400"), headers=headers
        ),
        api_world.client.post(
            f"/internal/ingest/{run_b.id}/progress", json=_progress("1000"), headers=headers
        ),
    )

    assert (first.status_code, second.status_code) == (204, 204), f"{first.text}\n{second.text}"
    state = await _watermark(api_world, run_a)
    assert state is not None
    # MỘT hàng duy nhất, và nó mang mốc CAO NHẤT — bất kể ai chạy trước.
    assert state.cursor_value == "1000"


async def test_the_first_write_race_window_is_actually_closed(api_world: ApiWorld) -> None:
    """Cửa sổ đua ép mở XÁC ĐỊNH, không nhờ may mắn về thời điểm.

    Gọi thẳng `_advance_watermark` với HAI session thật, thay vì hai request
    HTTP: session của một request được commit bên trong handler, nên không có
    cách nào tạm dừng nó ở giữa. Đây là cái giá phải trả — phép kiểm này chạm
    vào một hàm nội bộ — và nó đáng, vì phép kiểm HTTP ở trên không thể chứng
    minh cửa sổ đã được đi vào.

    Trình tự, và vì sao nó xác định:

    1. `first` chạy xong `INSERT ... ON CONFLICT DO NOTHING` nhưng CHƯA commit.
       Hàng đã tồn tại nhưng chưa hiện ra với bất kỳ transaction nào khác.
    2. `second` bắt đầu, và `_assert_parked_in_postgres` khẳng định nó KHÔNG
       chạy xong được — xem docstring hàm đó cho lý do đây là một quan sát chắc
       chắn về việc bị KHOÁ, không phải một phỏng đoán về thứ tự lập lịch.
    3. `first` commit. `second` tỉnh lại, thấy hàng đã có, và đi tiếp bằng
       `SELECT ... FOR UPDATE` + `moves_forward`.

    `wait_for` ở bước 3 có timeout để một deadlock đỏ trong mười giây thay vì
    treo cả bộ test.
    """
    run_a = await _run(api_world)
    run_b = await _sibling_run(api_world, run_a)

    maker = _maker(api_world)
    async with maker() as first, maker() as second:
        row_a = await first.get(IngestRun, run_a.id)
        row_b = await second.get(IngestRun, run_b.id)
        assert row_a is not None and row_b is not None

        await _advance_watermark(first, row_a, "id", "bigint", "400")

        blocked = asyncio.create_task(_advance_watermark(second, row_b, "id", "bigint", "1000"))
        await _assert_parked_in_postgres(blocked)

        await first.commit()
        await asyncio.wait_for(blocked, timeout=10)
        await second.commit()

    state = await _watermark(api_world, run_a)
    assert state is not None
    assert state.cursor_value == "1000"


async def test_a_concurrent_report_cannot_drag_the_watermark_backwards(
    api_world: ApiWorld,
) -> None:
    """Cùng cửa sổ, thứ tự NGƯỢC: người vào sau mang mốc THẤP HƠN.

    `ON CONFLICT DO NOTHING` một mình không đủ cho điều này — nó chỉ chặn được
    va chạm ở `INSERT`. Thứ giữ được luật "chỉ tiến" ở đây là `FOR UPDATE`:
    không có nó, `second` đọc watermark rồi ghi đè bằng một mốc thấp hơn dựa
    trên một giá trị đã cũ.
    """
    run_a = await _run(api_world)
    run_b = await _sibling_run(api_world, run_a)

    maker = _maker(api_world)
    async with maker() as first, maker() as second:
        row_a = await first.get(IngestRun, run_a.id)
        row_b = await second.get(IngestRun, run_b.id)
        assert row_a is not None and row_b is not None

        await _advance_watermark(first, row_a, "id", "bigint", "1000")
        blocked = asyncio.create_task(_advance_watermark(second, row_b, "id", "bigint", "400"))
        await _assert_parked_in_postgres(blocked)

        await first.commit()
        await asyncio.wait_for(blocked, timeout=10)
        await second.commit()

    state = await _watermark(api_world, run_a)
    assert state is not None
    assert state.cursor_value == "1000", (
        "Một lô đồng thời mang mốc thấp hơn đã kéo watermark lùi — đúng lớp mất "
        "dữ liệu âm thầm mà luật 'chỉ tiến' tồn tại để chặn."
    )


@pytest.mark.parametrize("cursor_type", ["text", "numeric", "character varying", "BIGINT", "int4"])
async def test_a_cursor_type_outside_the_allowlist_is_rejected(
    api_world: ApiWorld, cursor_type: str
) -> None:
    """422 ở BIÊN, và KHÔNG hàng `stream_state` nào.

    `"int4"`/`"BIGINT"` trong danh sách này có chủ đích: allowlist khớp CHÍNH
    XÁC chuỗi `information_schema.columns.data_type` trả về (chữ thường, có dấu
    cách), không phải bí danh `pg_catalog` và không phân biệt hoa thường.
    """
    run = await _run(api_world)
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress",
        json=_progress("400", cursor_type=cursor_type),
        headers=_headers(api_world),
    )
    assert response.status_code == 422, response.text
    assert await _watermark(api_world, run) is None


async def test_a_cursor_value_that_does_not_match_its_type_is_rejected(
    api_world: ApiWorld,
) -> None:
    """Một `cursor_type` hợp lệ không cứu được một `cursor_value` không đọc
    được dưới kiểu đó — và một watermark không parse được là một watermark
    không so sánh được, tức là luật "chỉ tiến" mất hiệu lực từ lô kế tiếp."""
    run = await _run(api_world)
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress",
        json=_progress("khong-phai-so"),
        headers=_headers(api_world),
    )
    assert response.status_code == 422, response.text
    assert await _watermark(api_world, run) is None


async def test_a_cursor_value_without_its_type_is_rejected(api_world: ApiWorld) -> None:
    """Ba trường cursor đi CÙNG NHAU hoặc không cái nào. Nhận `cursor_value`
    trần nghĩa là lưu một chuỗi mà lô sau không biết đọc bằng kiểu gì — đúng
    trạng thái mà `cursor_type` tồn tại để xoá bỏ."""
    run = await _run(api_world)
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress",
        json={"rows": 10, "cursor_value": "400"},
        headers=_headers(api_world),
    )
    assert response.status_code == 422, response.text
    assert await _watermark(api_world, run) is None


async def test_a_full_load_reports_rows_without_any_cursor(api_world: ApiWorld) -> None:
    """`mode="full"` không có watermark nào để báo — ba trường cursor vắng mặt
    phải là hợp lệ, không phải 422. Và KHÔNG được tạo ra một hàng
    `stream_state` rỗng."""
    run = await _run(api_world, mode="full")
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/progress", json={"rows": 250}, headers=_headers(api_world)
    )
    assert response.status_code == 204, response.text
    assert (await _run_row(api_world, run.id)).rows_written == 250
    assert await _watermark(api_world, run) is None


async def test_rows_written_accumulates_across_batches(api_world: ApiWorld) -> None:
    """`rows` là số dòng của LÔ NÀY, không phải tổng tích luỹ — pod không đọc
    lại `rows_written` nên nó không biết tổng. Một bản cài đặt GÁN thay vì CỘNG
    xanh với một lô duy nhất, nên phải có ít nhất hai lô khác số dòng."""
    run = await _run(api_world)
    for value, rows in (("400", 100), ("1000", 55)):
        await api_world.client.post(
            f"/internal/ingest/{run.id}/progress",
            json=_progress(value, rows=rows),
            headers=_headers(api_world),
        )
    assert (await _run_row(api_world, run.id)).rows_written == 155


# ----------------------------------------------------------------------- complete


async def test_complete_marks_the_run_succeeded(api_world: ApiWorld) -> None:
    run = await _run(api_world, status="running")
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/complete",
        json={"status": "succeeded"},
        headers=_headers(api_world),
    )
    assert response.status_code == 204, response.text
    row = await _run_row(api_world, run.id)
    assert row.status == "succeeded"
    assert row.error is None
    # `finished_at` là thứ phân biệt "đã xong" với "đang chạy" ở mọi màn hình
    # đọc bảng này; một run `succeeded` không có nó là một hàng nửa vời.
    assert row.finished_at is not None


async def test_complete_records_a_failure_and_its_reason(api_world: ApiWorld) -> None:
    run = await _run(api_world, status="running")
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/complete",
        json={"status": "failed", "error": "connection refused"},
        headers=_headers(api_world),
    )
    assert response.status_code == 204, response.text
    row = await _run_row(api_world, run.id)
    assert (row.status, row.error) == ("failed", "connection refused")
    assert row.finished_at is not None


async def test_a_failed_run_must_say_why(api_world: ApiWorld) -> None:
    """Pod bị dọn sau một giờ (`ttl_seconds_after_finished`, xem `jobs.py`), nên
    log của nó cũng biến mất — hàng `ingest_run` là thứ DUY NHẤT còn lại. Một
    `failed` không kèm lý do không dẫn người vận hành đi đâu cả."""
    run = await _run(api_world, status="running")
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/complete",
        json={"status": "failed"},
        headers=_headers(api_world),
    )
    assert response.status_code == 422, response.text
    assert (await _run_row(api_world, run.id)).status == "running"


async def test_an_unknown_completion_status_is_rejected(api_world: ApiWorld) -> None:
    """`status` là một `Literal` trên request model. `"cancelled"` cố ý nằm ở
    đây: `IngestRun` ghi rõ trạng thái đó KHÔNG tồn tại ở 3a, và nhận nó sẽ đưa
    vào cột `status` một giá trị mà không màn hình nào biết hiển thị."""
    run = await _run(api_world, status="running")
    response = await api_world.client.post(
        f"/internal/ingest/{run.id}/complete",
        json={"status": "cancelled"},
        headers=_headers(api_world),
    )
    assert response.status_code == 422, response.text
    assert (await _run_row(api_world, run.id)).status == "running"


# ---------------------------------------------------------------------- run không có


@pytest.mark.parametrize("route", ["spec", "progress", "complete"])
async def test_a_run_id_that_does_not_exist_is_404_not_500(api_world: ApiWorld, route: str) -> None:
    """Một run đã bị xoá (con người, hoặc vòng dọn của Task 13) là chuyện bình
    thường — câu trả lời đúng là "không có", không phải một stack trace.

    Cả ba route, vì `scalar_one()` (thứ ném `NoResultFound` -> 500) và
    `scalar_one_or_none()` trông giống hệt nhau ở chỗ gọi.
    """
    missing = uuid.uuid4()
    headers = _headers(api_world)
    if route == "spec":
        response = await api_world.client.get(f"/internal/ingest/{missing}/spec", headers=headers)
    elif route == "progress":
        response = await api_world.client.post(
            f"/internal/ingest/{missing}/progress", json=_progress("400"), headers=headers
        )
    else:
        response = await api_world.client.post(
            f"/internal/ingest/{missing}/complete",
            json={"status": "succeeded"},
            headers=headers,
        )
    assert response.status_code == 404, response.text
