"""`/internal/ingest/*` — ba đường mà POD NẠP gọi ngược về control plane.

Pod nạp không có credential Postgres nào và không có phiên nào: nó biết đúng
`run_id` của mình, một base URL, và một bí mật chia sẻ (xem `JobLauncher.
launch` — cả ba vào pod qua biến môi trường). Mọi thứ nó cần đọc và mọi thứ nó
cần ghi đi qua ba route ở đây, và không đường nào khác.

**Cổng ở cấp `APIRouter`, không lặp `Depends()` từng handler.** Ba route cần
đúng MỘT phép kiểm, và một handler thêm vào sau này (Task 13 rất có thể thêm
một đường báo heartbeat) tự động được bọc mà không ai phải nhớ dán dependency
vào nó. Cùng khuôn `loom_query.routers.query`. `require_ingest_secret` sống ở
`loom_api.internal_security` — đọc docstring module đó cho lý do bí mật NÀY
tách khỏi bí mật của `loom-query`, và cho giới hạn nó KHÔNG chống được.

**Router này tách khỏi `routers/internal.py` chứ không thêm vào đó, có chủ ý.**
Hai router cùng prefix gốc `/internal` nhưng khác hẳn nhau ở người gọi và ở
cách được bảo vệ: `routers/internal.py` phục vụ `loom-query`, KHÔNG kiểm gì, và
dựa vào ranh giới ingress. Gộp chung sẽ hoặc bắt `loom-query` gửi một header nó
không có (làm hỏng đường truy vấn Giai đoạn 2b), hoặc để `dependencies=` ở cấp
router phủ nửa số route và không phủ nửa kia — tức là biến "cả router được bọc"
thành một câu không còn đúng, đúng thứ mà việc đặt cổng ở cấp router tồn tại để
bảo đảm.

**404 cho `run_id` không tồn tại, không phải 500.** Một pod hỏi spec của một
run đã bị xoá là chuyện bình thường (con người xoá, hoặc vòng đối chiếu của
Task 13 dọn), và câu trả lời đúng là "không có" chứ không phải một stack trace.
`scalar_one_or_none` chứ không `scalar_one`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import SessionDep
from loom_api.internal_security import require_ingest_secret
from loom_api.models import IngestRun, Item, StreamState
from loom_core.cursor import moves_forward
from loom_core.item_definitions import ConnectionDefinition, ItemType
from loom_core.schemas import (
    IngestCompletionReport,
    IngestProgressReport,
    IngestSourceSpec,
    IngestSpec,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["internal"], dependencies=[Depends(require_ingest_secret)])


async def _run_or_404(session: AsyncSession, run_id: uuid.UUID) -> IngestRun:
    run = (
        await session.execute(select(IngestRun).where(IngestRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no ingest run with this id")
    return run


def _stream_state_query(run: IngestRun) -> Select[tuple[StreamState]]:
    """Watermark của stream này, tra bằng ĐÚNG ba cột của ràng buộc UNIQUE.

    `lakehouse_id` + `connection_id` + `stream`, không phải `run_id`: watermark
    thuộc về một STREAM chứ không về một lần chạy — đó là cả điểm của nó, và là
    lý do `stream_state` không có cột `run_id` nào (xem `models.py`). Tra theo
    ba cột này cũng có nghĩa là chỉ có tối đa một hàng khớp, do chính
    `uq_stream_state_lakehouse_connection_stream` bảo đảm.

    Trả về CÂU TRUY VẤN chứ không kết quả, vì hai chỗ gọi cần hai thứ khác nhau
    từ cùng một điều kiện lọc: `ingest_spec` chỉ ĐỌC, còn `_advance_watermark`
    cần `.with_for_update()`. Dựng điều kiện ở một chỗ là cách để chúng không
    trôi khỏi nhau — lệch một cột ở đây nghĩa là một trong hai đường đọc
    watermark của một stream KHÁC.
    """
    return select(StreamState).where(
        StreamState.lakehouse_id == run.lakehouse_id,
        StreamState.connection_id == run.connection_id,
        StreamState.stream == run.stream,
    )


async def _stream_state(session: AsyncSession, run: IngestRun) -> StreamState | None:
    return (await session.execute(_stream_state_query(run))).scalar_one_or_none()


@router.get("/{run_id}/spec", response_model=IngestSpec)
async def ingest_spec(run_id: uuid.UUID, session: AsyncSession = SessionDep) -> IngestSpec:
    """Cái gì cần nạp, từ đâu, và tiếp tục từ mốc nào.

    **Lời gọi này chuyển run từ `pending` sang `running`, và đó là một GET CÓ
    TÁC DỤNG PHỤ — một sự đánh đổi có chủ ý, không phải một sơ suất.** Lấy spec
    là hành động ĐẦU TIÊN của pod, nên nó là thời điểm sớm nhất mà control plane
    biết được pod đã chạy thật.

    Nói cho đúng: đây KHÔNG phải tín hiệu duy nhất tồn tại. `job_name` tất định
    theo `run_id` và `JobLauncher.status()` trả về `exists`/`active`/`succeeded`/
    `failed` (xem `jobs.py`), nên một vòng đối chiếu phân biệt được "pending +
    Job đang chạy" với "pending + không có Job nào" mà KHÔNG cần ai ghi vào
    database. Lập luận giữ chuyển tiếp này vì vậy hẹp hơn: nó làm cột `status`
    tự nói đúng sự thật cho MỌI người đọc bảng `ingest_run` (giao diện Task 13,
    một câu SQL tay, một lần điều tra sự cố) chứ không chỉ cho một vòng đối
    chiếu có với tới được Kubernetes, và nó không tốn thêm round trip nào.

    Hai phương án còn lại đều tệ hơn. Để `/progress` chuyển trạng thái: một lô
    đầu tiên lớn nghĩa là nhiều phút im lặng, và suốt thời gian đó bảng nói
    "Job chưa bao giờ khởi động" về một pod đang chạy khoẻ. Thêm một endpoint
    `/start` thứ tư: một round trip nữa để nói đúng cái điều mà request lấy spec
    đã chứng minh.

    Giới hạn kèm theo — `running` nghĩa là "đã lấy spec ít nhất một lần", KHÔNG
    phải "pod còn sống" — ghi ở docstring `IngestRun` (`models.py`), cạnh đoạn
    về `pending`, vì đó là chỗ người làm Task 13 sẽ đọc.

    Chỉ `pending` -> `running`, không đụng tới `succeeded`/`failed`: gọi lại
    spec sau khi đã kết thúc (một lần thử lại, một lần gọi nhầm) không được
    hồi sinh một run đã đóng.

    Connection tra bằng `id`, KHÔNG kèm `state == ACTIVE`: xoá mềm connection
    trong lúc một run đang chạy là chuyện xảy ra được, và câu trả lời đúng cho
    pod là spec nó đang cần để chạy nốt, không phải một 404 giữa chừng làm run
    chết ở một chỗ không ai đoán ra. Việc CHẶN một run MỚI dùng connection đã
    xoá đã nằm ở `routers/ingest.py` (`_active_item` có lọc `ACTIVE`) — đúng
    chỗ của nó, ở biên tạo run.
    """
    run = await _run_or_404(session, run_id)

    connection = (
        await session.execute(
            select(Item).where(Item.id == run.connection_id, Item.type == str(ItemType.connection))
        )
    ).scalar_one_or_none()
    if connection is None:
        # KHÔNG với tới được qua đường bình thường, và cả HAI vế của điều kiện
        # tra ở trên đều có lý do: khoá ngoại `ingest_run.connection_id ->
        # item.id` khiến hàng `item` không xoá cứng được chừng nào run còn
        # sống (xoá MỀM chỉ đổi `state`, và câu tra này cố ý không lọc theo
        # `state` — xem docstring), còn lọc `type = 'connection'` bắt trường
        # hợp một hàng `ingest_run` trỏ vào một item KHÁC loại: `routers/
        # ingest.py` không tạo ra được hàng như thế, nhưng một lần sửa tay hay
        # một thành phần sau này thì có.
        #
        # 409 chứ không 404: `run_id` TỒN TẠI, và trả 404 ở đây sẽ khiến pod
        # (và người đọc log) đi tìm một run không có thật.
        logger.error("ingest.spec_connection_missing", run_id=str(run_id))
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the connection this run was created from no longer exists"
        )
    try:
        definition = ConnectionDefinition.model_validate(connection.definition)
    except ValidationError as exc:
        # Cùng lập luận với `routers/ingest.py`: `definition` hỏng là DỮ LIỆU ĐÃ
        # LƯU sai, không phải request sai, nên không để nó rơi vào
        # `_pydantic_validation_handler` (handler đó gắn `loc: ["body", …]` vào
        # một thân request mà đường GET này thậm chí không có).
        logger.error("ingest.spec_connection_definition_invalid", run_id=str(run_id))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this connection's definition is not usable — open the connection and re-save it",
        ) from exc

    state = await _stream_state(session, run)
    # `mode="full"` KHÔNG gửi watermark đi, dù hàng `stream_state` có thể đang
    # tồn tại từ những lần nạp gia tăng trước: "full" nghĩa là đọc lại từ đầu,
    # và đưa một watermark cho pod ở chế độ đó là mời nó lặng lẽ làm một lần
    # nạp gia tăng dưới cái tên "full". Hàng cũ vẫn NGUYÊN VẸN — lần nạp
    # incremental tiếp theo dùng lại nó đúng ở mốc cũ.
    #
    # SỬA Ở TASK 12: câu cuối của chú thích này từng nói "lần full này vẫn đẩy
    # watermark lên qua `/progress` như thường". Điều đó KHÔNG còn đúng — và
    # cũng chưa bao giờ nên đúng: `run_full` không gọi `/progress` một lần nào
    # (spec mục 5, "full không đụng tới stream_state", và
    # `test_full_does_not_touch_the_watermark`). Hệ quả phải nói ra vì nó nhìn
    # thấy được từ ngoài: cột `ingest_run.rows_written` của một run `full` đứng
    # ở 0 tới lúc run kết thúc, vì `/progress` là đường DUY NHẤT cộng dồn nó và
    # `/complete` cố ý không mang `rows` (xem `IngestCompletionReport`). Một
    # thanh tiến trình cho mode `full` vì vậy chưa có số để hiển thị.
    watermark = state if (run.mode == "incremental" and state is not None) else None

    spec = IngestSpec(
        run_id=run.id,
        lakehouse_id=run.lakehouse_id,
        workspace_id=run.workspace_id,
        # Lấy từ hàng `ingest_run`, không từ `connection.id` vừa tra ở trên: hai
        # giá trị luôn bằng nhau (câu tra dùng chính `run.connection_id`), nhưng
        # đọc từ `run` nói đúng nguồn sự thật — cột `_source` của bronze phải ghi
        # connection mà RUN NÀY được tạo với, không phải hàng item nào tình cờ
        # được tra ra ở đây.
        connection_id=run.connection_id,
        stream=run.stream,
        # `run.mode` là `str` ở tầng ORM (cột `String(16)`) nhưng `IngestSpec.
        # mode` là `Literal["full", "incremental"]`, nên phải thu hẹp ở đây.
        # Một giá trị thứ ba không vào được bảng qua đường bình thường
        # (`IngestStartRequest.mode` là CÙNG `Literal` đó), và nếu nó vào được
        # bằng cách nào khác thì rơi về `"full"` là hướng AN TOÀN của hai
        # hướng: đọc lại từ đầu sinh trùng lặp (đếm được, khử được ở silver),
        # còn đoán nhầm thành `"incremental"` sẽ bỏ sót dữ liệu không dấu vết.
        mode="incremental" if run.mode == "incremental" else "full",
        source=IngestSourceSpec(
            kind=definition.kind,
            host=definition.host,
            port=definition.port,
            database=definition.database,
        ),
        cursor_column=watermark.cursor_column if watermark else None,
        cursor_type=watermark.cursor_type if watermark else None,
        cursor_value=watermark.cursor_value if watermark else None,
    )

    # Dựng phản hồi XONG rồi mới ghi trạng thái: một run bị đánh dấu `running`
    # trong khi lời gọi trả về lỗi là một hàng nói dối — pod không hề nhận được
    # spec nào và sẽ không chạy, nhưng vòng đối chiếu của Task 13 lại thấy
    # `running` và để yên.
    if run.status == "pending":
        run.status = "running"
        await session.commit()
    return spec


@router.post("/{run_id}/progress", status_code=status.HTTP_204_NO_CONTENT)
async def ingest_progress(
    run_id: uuid.UUID, body: IngestProgressReport, session: AsyncSession = SessionDep
) -> None:
    """Cộng dồn số dòng, và đẩy watermark LÊN — không bao giờ xuống.

    `rows_written` cộng dồn bằng `+=` trên giá trị vừa đọc trong CÙNG
    transaction của request này. Đủ ở 3a vì mỗi run có đúng một pod
    (`backoff_limit=0`, xem `jobs.py`) và pod đó gửi các lô TUẦN TỰ, nên không
    có hai lần cộng nào chồng nhau trên cùng một hàng. Cái làm câu đó hết đúng
    là một pod gửi song song hoặc một run có nhiều pod — lúc đó phải đổi sang
    `UPDATE ... SET rows_written = rows_written + :n` để phép cộng chạy trong
    Postgres. Ghi ra đây vì hai dòng code trông y hệt nhau nhưng bảo đảm khác
    hẳn nhau.
    """
    run = await _run_or_404(session, run_id)
    run.rows_written += body.rows

    if body.cursor_column is not None and body.cursor_type is not None:
        assert body.cursor_value is not None  # `_cursor_fields_travel_together` đã bảo đảm
        await _advance_watermark(
            session, run, body.cursor_column, body.cursor_type, body.cursor_value
        )

    await session.commit()


async def _advance_watermark(
    session: AsyncSession,
    run: IngestRun,
    cursor_column: str,
    cursor_type: str,
    cursor_value: str,
) -> None:
    """Luật "chỉ tiến", và HAI trường hợp mà nó không áp dụng được.

    **Đổi `cursor_column` -> ĐẶT LẠI, không so sánh.** Hai giá trị nằm trên hai
    THANG ĐO khác nhau (`id` = 91234 với `updated_at` = 2026-08-13), nên "lớn
    hơn" giữa chúng không mang nghĩa gì — nó chỉ tình cờ đúng hoặc tình cờ sai.
    So sánh ở đây sẽ hoặc khoá chết watermark mới ở một mốc cũ khổng lồ, hoặc
    cho qua một mốc vô nghĩa. Đây cũng chính là lý do `uq_stream_state_
    lakehouse_connection_stream` CỐ Ý không có `cursor_column` trong khoá (xem
    docstring `StreamState`): một hàng cho mỗi stream, và đổi cột thì hàng đó
    được viết lại chứ không sinh thêm hàng thứ hai để lần sau chọn bừa.

    **Đổi `cursor_type` -> cũng ĐẶT LẠI, cùng lập luận.** Kể cả khi tên cột
    không đổi: `parse_cursor_value` đọc giá trị cũ dưới kiểu MỚI sẽ hoặc ném
    (`"2026-08-13"` dưới `bigint`) hoặc — tệ hơn — thành công với một nghĩa
    khác. Trường hợp thường gặp nhất của nhánh này lại rất tầm thường:
    `cursor_type` là `NULL` cho mọi hàng có từ trước migration 0006.

    Ngoài hai trường hợp đó thì `moves_forward` quyết định, và nó so theo KIỂU
    chứ không theo chuỗi — xem `loom_core.cursor` cho lý do khác biệt đó là
    toàn bộ vấn đề.

    **HAI run song song trên cùng một stream là chuyện XẢY RA ĐƯỢC, nên hàm này
    hiện thực-hoá-rồi-khoá thay vì `SELECT` rồi `INSERT`.** `start_ingest` không
    có cổng chống trùng, và `job_name` tất định theo `run_id` chứ không theo
    stream, nên hai lần bấm Nạp cho cùng `(lakehouse, connection, stream)` cho
    ra hai Job cùng ghi một stream. Với `SELECT`-rồi-`INSERT`, hai lần báo tiến
    độ ĐẦU TIÊN chạy đan nhau đều thấy `None` và cùng `INSERT`: một cái vỡ ở
    `uq_stream_state_lakehouse_connection_stream` -> `IntegrityError` -> 500
    (đã dựng lại thật qua ASGI, không phải suy luận).

    `INSERT ... ON CONFLICT DO NOTHING` rồi `SELECT ... FOR UPDATE`: sau câu
    đầu, hàng CHẮC CHẮN tồn tại (ta vừa tạo, hoặc nó đã có); câu thứ hai khoá
    đúng hàng đó tới hết transaction, nên hai lời gọi đồng thời được Postgres
    xếp thành tuần tự và phép so "chỉ tiến" đọc một giá trị không ai đang sửa.
    Khi câu `INSERT` thắng, `moves_forward` so giá trị với CHÍNH NÓ (`<` nên
    `False`) và không có `UPDATE` nào phát sinh — hàng đã đúng rồi.

    **Vì sao KHÔNG viết luật "chỉ tiến" vào SQL** (`ON CONFLICT DO UPDATE ...
    WHERE excluded.cursor_value > stream_state.cursor_value`), dù nó gọn hơn:
    `cursor_value` là `Text`, nên một phép so trong SQL trên cột đó là ĐÚNG con
    so-sánh-chuỗi mà cả nhiệm vụ này tồn tại để diệt — `'1000' > '400'` là
    `false` ở Postgres y như ở Python. Viết đúng đòi `CAST` theo từng
    `cursor_type` ngay trong câu lệnh, tức là chuyển bảng phân nhánh của
    `parse_cursor_value` vào SQL và nhân đôi nó. Khoá bi quan đắt hơn một chút
    và giữ phép so ở đúng một chỗ, có kiểu, kiểm được không cần database.

    Cái này KHÔNG chữa việc trùng lặp: hai Job vẫn cùng đọc và cùng ghi bronze
    bất kể ai thắng watermark, và cổng chống trùng ở `start_ingest` đáng giá
    hơn hẳn cái khoá này — khoảng trống đã ghi tên ở docstring `IngestRun`, chỗ
    Task 13 sẽ đọc.
    """
    # `id` sinh ở client chứ không dựa `default=uuid.uuid4` của model: đây là
    # một câu INSERT Core, không đi qua ORM instrumentation nên default của
    # `mapped_column` không được áp. `updated_at` thì KHÔNG cần — nó có
    # `server_default=func.now()`, do Postgres điền.
    await session.execute(
        pg_insert(StreamState)
        .values(
            id=uuid.uuid4(),
            lakehouse_id=run.lakehouse_id,
            connection_id=run.connection_id,
            stream=run.stream,
            cursor_column=cursor_column,
            cursor_type=cursor_type,
            cursor_value=cursor_value,
        )
        .on_conflict_do_nothing(constraint="uq_stream_state_lakehouse_connection_stream")
    )
    # `scalar_one`, KHÔNG `scalar_one_or_none`: câu trên vừa bảo đảm hàng tồn
    # tại, nên "không có hàng" ở đây là một giả định đã vỡ, không phải một
    # trạng thái hợp lệ để đi tiếp bằng một nhánh `None` im lặng.
    state = (await session.execute(_stream_state_query(run).with_for_update())).scalar_one()

    rescaled = state.cursor_column != cursor_column or state.cursor_type != cursor_type
    if rescaled:
        logger.info(
            "ingest.watermark_reset",
            run_id=str(run.id),
            stream=run.stream,
            was=(state.cursor_column, state.cursor_type),
            now=(cursor_column, cursor_type),
        )
    if rescaled or moves_forward(cursor_type, state.cursor_value, cursor_value):
        state.cursor_column = cursor_column
        state.cursor_type = cursor_type
        state.cursor_value = cursor_value
        # `updated_at` chỉ nhúc nhích khi watermark THẬT SỰ đổi: cột đó là cách
        # duy nhất đọc được "stream này tiến lần cuối lúc nào", và chạm vào nó
        # ở mỗi lô sẽ làm một stream đứng yên trông như đang tiến đều.
        state.updated_at = datetime.now(UTC)


@router.post("/{run_id}/complete", status_code=status.HTTP_204_NO_CONTENT)
async def ingest_complete(
    run_id: uuid.UUID, body: IngestCompletionReport, session: AsyncSession = SessionDep
) -> None:
    """Đóng run. `finished_at` đặt Ở ĐÂY, không phải bằng `server_default`.

    Cột `finished_at` nullable và không có mặc định (migration 0005) đúng là vì
    thời điểm kết thúc chỉ biết được ở lời gọi này — `now()` của Postgres tại
    đây và đồng hồ của pod chênh nhau không đáng kể so với độ dài một lần nạp,
    nên lấy đồng hồ của tiến trình API là đủ và đọc được ngay mà không cần một
    lượt refresh.

    KHÔNG chặn một run đã kết thúc bị đóng lại lần nữa. Với `backoff_limit=0`
    mỗi run chỉ có một pod, nên trường hợp đó là một lần gửi lại của chính pod
    đó (mạng chập) — và ghi đè cùng một kết quả lên chính nó là vô hại. Thêm
    một cổng "đã đóng rồi" ở đây sẽ biến một lần gửi lại vô hại thành một lỗi
    mà pod phải biết cách phân biệt với một lỗi thật.
    """
    run = await _run_or_404(session, run_id)
    run.status = body.status
    run.error = body.error
    run.finished_at = datetime.now(UTC)
    await session.commit()
