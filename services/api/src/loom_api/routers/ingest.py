"""`POST /api/v1/lakehouses/{lakehouse_id}/ingest` — bắt đầu một lần nạp.

**Cổng quyền hỏi `item.update` TRÊN LAKEHOUSE.** Nạp dữ liệu là GHI, và thứ bị
ghi là lakehouse. Đây chính là lỗ mà Giai đoạn 2c phát hiện ở CTAS:
`ACTION_MATRIX` xếp `item.update` vào `contributor`, nhưng cổng quyền lúc đó
chỉ đòi `viewer`, nên một viewer tạo được bảng. Phép kiểm
`test_a_viewer_cannot_start_an_ingest` canh đúng chỗ đó, và nó chứng minh đỏ
được bằng cách hạ hằng số dưới đây xuống `Action.item_read`.

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

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import State

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.ingest_service import JobLauncherLike, SecretRefUnusable, secret_name_for
from loom_api.jobs import JobLauncher
from loom_api.models import ACTIVE, IngestRun, Item
from loom_api.permissions import PermissionService
from loom_core.config import Settings
from loom_core.item_definitions import ConnectionDefinition, ItemType
from loom_core.roles import Action
from loom_core.schemas import IngestRunAccepted, IngestStartRequest, Principal

router = APIRouter(tags=["ingest"])

# Hằng số có TÊN, không phải một tham số nội tuyến ở dòng gọi: bước "chứng minh
# đỏ" của Task 9 hạ đúng dòng này xuống `Action.item_read` và chạy lại
# `test_a_viewer_cannot_start_an_ingest`. Một chỗ để sửa, một chỗ để đọc.
WRITE_TO_LAKEHOUSE = Action.item_update


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
    kia. Vô hại: `JobLauncher` không giữ trạng thái nào ngoài cấu hình, và
    `load_*_config` chỉ nạp lại cùng một file. Một khoá ở đây sẽ mua đúng một
    lần đọc file tiết kiệm được, đổi lấy một điểm đồng bộ nữa để hiểu sai.
    """
    launcher: JobLauncherLike | None = getattr(app_state, "job_launcher", None)
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
    await perms.require_item(lakehouse_id, WRITE_TO_LAKEHOUSE)

    connection = await _active_item(session, body.connection_id, ItemType.connection)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no connection with this id")
    await perms.require_item(body.connection_id, Action.item_read)

    # `ValidationError` của pydantic ở đây thành 422 qua
    # `_pydantic_validation_handler` (xem `errors.py`) — đúng hình dạng lỗi mà
    # frontend đã biết đọc. Một `connection` có `definition` không parse được
    # là item hỏng, không phải sự cố server, nên 422 nói đúng chuyện gì xảy ra.
    definition = ConnectionDefinition.model_validate(connection.definition)
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
    # `run_in_threadpool` (chính là `anyio.to_thread.run_sync` mà Starlette
    # dùng cho handler đồng bộ) trả event loop lại trong lúc chờ. Nói rõ nó
    # KHÔNG hứa gì: thread pool bị chặn ở 40 token mặc định của anyio, nên một
    # đợt nạp ồ ạt sẽ XẾP HÀNG — đó là back-pressure, không phải thread mọc vô
    # hạn, nhưng cũng nghĩa là request thứ 41 chờ. Và không có timeout nào ở
    # đây: `kubernetes` không đặt timeout mặc định, nên một API server treo giữ
    # một thread cho tới khi socket tự đứt. Cả hai đều chấp nhận được ở nhịp
    # "một người bấm nút Nạp"; cả hai đều phải xem lại nếu có thứ tự sinh run.
    #
    # Một `ApiException` không phải 409 nổi lên thành 500 (xem `launch()`):
    # hàng `ingest_run` đã commit ở trên nên ý định KHÔNG mất, và Task 13 dọn
    # nó. Nuốt lỗi ở đây để vẫn trả 202 mới là điều sai — 202 nghĩa là "đã nhận
    # và đã yêu cầu Job", và nói thế khi chưa yêu cầu được là một lời nói dối
    # mà người dùng chỉ phát hiện ra khi run treo mãi ở `pending`.
    await run_in_threadpool(_launch, request.app.state, settings, run.id, secret_name)
    return IngestRunAccepted(run_id=run.id)
