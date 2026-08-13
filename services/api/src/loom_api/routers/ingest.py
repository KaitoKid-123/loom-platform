"""`POST /api/v1/lakehouses/{lakehouse_id}/ingest` — bắt đầu một lần nạp.

**Cổng quyền hỏi `item.update` TRÊN LAKEHOUSE.** Nạp dữ liệu là GHI, và thứ bị
ghi là lakehouse. Đây chính là lỗ mà Giai đoạn 2c phát hiện ở CTAS:
`ACTION_MATRIX` xếp `item.update` vào `contributor`, nhưng cổng quyền lúc đó
chỉ đòi `viewer`, nên một viewer tạo được bảng. Phép kiểm
`test_a_viewer_cannot_start_an_ingest` canh đúng chỗ đó, và nó chứng minh đỏ
được bằng cách hạ `Action.item_update` ở `start_ingest` xuống `Action.item_read`.

**`connection_id` cũng phải qua cổng, bằng `item.read`.** Contributor trên
lakehouse L không tự động là người được mượn MỌI connection: nếu chỉ kiểm L,
một người gõ được id của một connection ở workspace họ không thấy sẽ có một Job
gắn vào k8s Secret của người khác — credential đi mượn qua một id đoán được.
`item.read` chứ không `item.update`: dùng một connection làm nguồn không sửa gì
trong nó, và đòi contributor trên connection sẽ khoá mất trường hợp bình thường
(một connection dùng chung, chia sẻ ở mức xem cho nhiều nhóm nạp).

**Thứ tự: tra hàng TRƯỚC, hỏi quyền SAU, và cả hai nhánh hỏng đều là 404.** Một
id không tồn tại và một id tồn tại nhưng người gọi không thấy phải không phân
biệt được từ ngoài — xem `NotVisible` ở `permissions.py`. Vì cả hai nhánh cùng
trả 404 nên thứ tự này không rò rỉ gì, và nó tiết kiệm một lượt hỏi quyền cho
một id vô nghĩa.

**KHÔNG có cổng chống trùng, và đó là một khoảng trống đã biết chứ không phải
một quyết định.** Không gì ở đây ngăn hàng `ingest_run` thứ hai cho cùng
`(lakehouse_id, connection_id, stream)` khi lần trước còn `pending`/`running`,
và `job_name` tất định theo `run_id` chứ không theo stream — nên hai lần bấm Nạp
cho ra HAI Job cùng đọc nguồn và cùng ghi một bảng bronze. Task 10 đã làm
`_advance_watermark` an toàn với đua (xem `routers/internal_ingest.py`) nên
không ai nhận 500 và watermark không lùi, nhưng đó chỉ chữa phần watermark:
công việc vẫn bị làm hai lần. Cách chữa là một 409 Ở ĐÂY — lý do đầy đủ nằm ở
docstring `IngestRun` (`models.py`), chỗ Task 13 sẽ đọc.

**Ghi Postgres TRƯỚC, phóng Job SAU, và không đảo lại được.** Hàng
`ingest_run` là *ý định*; Job chỉ là cách ý định đó thành sự thật (xem
docstring `jobs.job_name`). Commit trước nghĩa là một lần `launch()` hỏng để
lại một run `pending` không có Job — đúng trạng thái mà vòng đối chiếu lười của
Task 13 sinh ra để dọn, và một lần submit lại là vô hại vì tên Job tất định
theo `run_id`. Đảo thứ tự thì một commit hỏng để lại một pod ĐANG CHẠY đi hỏi
spec của một run không tồn tại: không ai dọn được thứ không có hàng nào trong
Postgres.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.ingest_service import SecretRefUnusable, secret_name_for
from loom_api.jobs import JobLauncher, JobLauncherLike
from loom_api.models import ACTIVE, IngestRun, Item
from loom_api.permissions import PermissionService
from loom_core.config import Settings
from loom_core.item_definitions import ConnectionDefinition, ItemType
from loom_core.roles import Action
from loom_core.schemas import IngestRunAccepted, IngestStartRequest, Principal

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["ingest"])


async def _active_item(
    session: AsyncSession, item_id: uuid.UUID, item_type: ItemType
) -> Item | None:
    """Hàng `item` đang sống ĐÚNG loại này, hoặc `None`.

    Không dùng lại `routers/query.py::_lakehouse_workspace_id` (cùng ba điều
    kiện lọc) vì hàm đó chỉ trả `workspace_id` của một `lakehouse`, còn ở đây
    cần cả hàng — `definition` của connection là nơi `secret_ref` nằm — và cần
    nó cho HAI loại item. Ba điều kiện `id`/`type`/`state` không mã hoá luật
    nào để trôi khỏi nhau; luật quyền nằm ở `PermissionService`, và cả hai
    đường đều gọi nó chứ không tự tính.
    """
    stmt = select(Item).where(
        Item.id == item_id,
        Item.type == str(item_type),
        Item.state == ACTIVE,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _launch(app_state: State, settings: Settings, run_id: uuid.UUID, secret_name: str) -> None:
    """Phóng Job cho `run_id`. ĐỒNG BỘ, và chỉ được gọi từ một thread.

    Dựng `JobLauncher` LƯỜI, ở lần nạp đầu tiên, chứ không ở `create_app()`:
    `JobLauncher.__init__` gọi `load_incluster_config()` rồi rơi về
    `load_kube_config()` (xem `jobs.py`), nên dựng nó lúc tạo app sẽ giết MỌI
    `create_app()` trên máy không có kubeconfig — CI, và mọi unit test hiện có,
    không riêng test của đường nạp. Cái giá là một kubeconfig hỏng lộ ra ở
    request nạp đầu tiên thay vì lúc khởi động; đổi lại, phần còn lại của
    control plane vẫn phục vụ được khi cụm k8s không với tới, điều đúng cho một
    API mà nạp dữ liệu chỉ là một đường trong nhiều đường.

    Hai request đồng thời có thể cùng dựng một launcher và một cái ghi đè cái
    kia. Kết quả không phụ thuộc thứ tự, nhưng KHÔNG phải vì `load_*_config`
    chỉ đọc: nó GHI vào biến toàn cục của tiến trình
    (`kubernetes.client.Configuration._default`, qua `set_default` — một
    deepcopy), và `load_kube_config` mặc định `persist_config=True` nên có thể
    ghi lại `~/.kube/config` khi auth provider làm mới token. Vô hại vì hai lần
    gọi đọc cùng một nguồn và cho ra hai cấu hình tương đương, nên cái nào
    thắng cũng như nhau — và đường trong-cụm (`load_incluster_config`, đường
    DUY NHẤT chạy ở dev/prod) không ghi file nào. Một khoá ở đây mua đúng một
    lần đọc file, đổi lấy một điểm đồng bộ nữa để hiểu sai.
    """
    # Đọc thẳng thuộc tính, KHÔNG `getattr(..., None)`: `create_app` luôn đặt
    # nó (dù là None), nên một `getattr` có mặc định chỉ che được đúng trường
    # hợp tên bị đổi ở một trong hai chỗ — và che bằng cách lặng lẽ dựng một
    # `JobLauncher` THẬT, biến một lỗi gõ nhầm thành một lời gọi k8s sống
    # trong test. `AttributeError` ồn ào là câu trả lời đúng cho việc đó.
    launcher: JobLauncherLike | None = app_state.job_launcher
    if launcher is None:
        launcher = JobLauncher(
            namespace=settings.task_namespace,
            image=settings.task_image,
            api_base_url=settings.task_api_base_url,
        )
        app_state.job_launcher = launcher
    launcher.launch(
        run_id,
        secret_name,
        (settings.task_shared_secret_name, settings.task_shared_secret_key),
        cpu=settings.task_cpu,
        memory=settings.task_memory,
    )


@router.post(
    "/lakehouses/{lakehouse_id}/ingest",
    response_model=IngestRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_ingest(
    lakehouse_id: uuid.UUID,
    body: IngestStartRequest,
    request: Request,
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> IngestRunAccepted:
    settings: Settings = request.app.state.settings
    perms = PermissionService(session, principal)

    lakehouse = await _active_item(session, lakehouse_id, ItemType.lakehouse)
    if lakehouse is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lakehouse with this id")
    await perms.require_item(lakehouse_id, Action.item_update)

    connection = await _active_item(session, body.connection_id, ItemType.connection)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no connection with this id")
    await perms.require_item(body.connection_id, Action.item_read)

    # Một `connection` mà `definition` không parse được là dữ liệu ĐÃ LƯU bị
    # hỏng, KHÔNG phải thân request sai — nên không để nó rơi vào
    # `_pydantic_validation_handler` (handler đó gắn `loc: ["body", …]` và câu
    # "the submitted data is not valid", cả hai đều sai ở đây: thân request hợp
    # lệ, và `IngestStartRequest` không có ô `secret_ref` nào để frontend tô).
    # Xem docstring của handler đó ở `errors.py` — nó tự mô tả mình là dành cho
    # `definition` đi vào TRONG thân request, đúng thứ không xảy ra trên đường
    # này.
    #
    # 409 chứ không 422: không có gì người gọi sửa được trong request; thứ phải
    # sửa là chính item connection, và thông báo nói đúng điều đó.
    try:
        definition = ConnectionDefinition.model_validate(connection.definition)
    except ValidationError as exc:
        logger.error("ingest.connection_definition_invalid", connection_id=str(connection.id))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this connection's definition is not usable — open the connection and re-save it",
        ) from exc

    try:
        secret_name = secret_name_for(definition.secret_ref, settings.task_namespace)
    except SecretRefUnusable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    run = IngestRun(
        id=uuid.uuid4(),
        lakehouse_id=lakehouse_id,
        # `workspace_id` lấy TỪ LAKEHOUSE, không bao giờ từ client — cùng quy
        # tắc mà `routers/query.py` ghi ở docstring của nó. Ở đây client thậm
        # chí không có ô nào để khai (xem `IngestStartRequest`).
        connection_id=body.connection_id,
        workspace_id=lakehouse.workspace_id,
        stream=body.stream,
        mode=body.mode,
        # Tường minh chứ không dựa `server_default`: `run.status` phải đọc được
        # ngay mà không cần một lượt refresh, và một trạng thái khởi đầu quan
        # trọng tới mức này nên nằm trong mã người đọc đang xem.
        status="pending",
    )
    session.add(run)
    await session.commit()

    # `launch()` là ĐỒNG BỘ và chặn: client `kubernetes` dùng urllib3, không có
    # bản async. Gọi thẳng trong handler này sẽ giữ event loop suốt một round
    # trip tới API server — với một tiến trình phục vụ mọi request của mọi
    # người, đó là cái giá sai chỗ.
    #
    # `asyncio.to_thread` — CÙNG cơ chế `item_store.create` dùng cho
    # `provision_warehouse` (một round trip `httpx.Client` chặn, xem docstring
    # ở đó) và cùng cơ chế `loom-query` dùng ở ba chỗ. Một cơ chế cho một bài
    # toán: `starlette.concurrency.run_in_threadpool` giải đúng việc này nhưng
    # bằng pool KHÁC (limiter 40 token của anyio thay vì executor mặc định của
    # event loop), và hai pool cạnh nhau nghĩa là mọi câu nói về "giới hạn bao
    # nhiêu" đúng ở nửa số chỗ gọi.
    #
    # Nói rõ nó KHÔNG hứa gì. Executor mặc định là `min(32, cpu+4)` thread
    # (12 trên máy đo), và hàng đợi phía sau nó KHÔNG chặn — quá số đó thì việc
    # xếp hàng không giới hạn chứ không có back-pressure nào đẩy ngược lên
    # người gọi. Task ĐANG chờ thì huỷ được (client ngắt kết nối thu lại được
    # task), nhưng THREAD thì không: nó chạy tiếp tới hết, tách rời — đúng điều
    # `loom_query.runner` đã ghi. Và không có timeout nào: `kubernetes` không
    # đặt mặc định, nên một API server bị black-hole giữ một thread cho tới khi
    # TCP tự bỏ, còn `loop.shutdown_default_executor()` (bước tắt chuẩn của
    # `asyncio.run`, đường uvicorn đi) chờ đúng thread đó. Chấp nhận được ở
    # nhịp "một người bấm nút Nạp"; phải đặt `_request_timeout` cho `launch()`
    # trước khi có thứ TỰ SINH run.
    #
    # Một `ApiException` không phải 409 nổi lên thành 500 (xem `launch()`):
    # hàng `ingest_run` đã commit ở trên nên ý định KHÔNG mất, và vòng đối
    # chiếu lười của Task 13 sinh ra để dọn đúng trạng thái đó (nó CHƯA tồn
    # tại — tới lúc đó, một run như vậy nằm lại ở `pending`). Nuốt lỗi ở đây để
    # vẫn trả 202 mới là điều sai — 202 nghĩa là "đã nhận và đã yêu cầu Job",
    # và nói thế khi chưa yêu cầu được là một lời nói dối mà người dùng chỉ
    # phát hiện ra khi run treo mãi ở `pending`.
    await asyncio.to_thread(_launch, request.app.state, settings, run.id, secret_name)
    return IngestRunAccepted(run_id=run.id)
