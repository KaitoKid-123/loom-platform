"""Bí mật chia sẻ mà `loom-api` KIỂM, không phải bí mật nó gửi đi.

Giai đoạn 2b chỉ xây một chiều: `loom-api` ĐÍNH `X-Loom-Query-Secret` vào mọi
request gửi sang `loom-query`, và `loom_query.security.require_shared_secret`
kiểm nó BÊN ĐÓ. Router `/internal` của chính `loom-api` (`routers/internal.py`)
không kiểm gì cả — nó dựa vào ranh giới MẠNG, và docstring của nó nói thẳng
điều đó: ingress chỉ chuyển `/api` tới service này, nên `/internal/*` không có
đường vào từ ngoài cluster.

Task 10 là lần đầu `loom-api` phải kiểm một bí mật ĐI VÀO, và lý do nó không
dùng lại được lập luận "ranh giới mạng là đủ" ở trên: người gọi
`/internal/ingest/*` là một Job do CHÍNH `loom-api` phóng ra, chạy trong cùng
namespace, và Job đó là thành phần duy nhất quay số ra một
`ConnectionDefinition.host` do người dùng nhập. Ranh giới mạng đã chặn hết bên
ngoài; thứ còn lại cần chặn là bên TRONG namespace — một pod bất kỳ ghi bừa
watermark hay đánh dấu run của người khác là `failed`.

**KHÔNG áp phép kiểm này lên `/internal/authz/items` hay
`/internal/lakehouses/resolve`.** `loom-query` gọi hai đường đó và không gửi
header nào cả (xem `loom_query.authz`); thêm cổng vào đó là làm hỏng đường
truy vấn của Giai đoạn 2b. Chúng nằm trong phạm vi rà lại của Giai đoạn 6, khi
principal được KÝ thay vì được tin.

**GIỚI HẠN CÒN LẠI, ghi ra chứ không ngụ ý là đã hết:** bí mật này chứng minh
"một pod nạp gửi request này" — KHÔNG phải "pod nạp của ĐÚNG run này". Mọi Job
nạp nhận cùng một giá trị qua `secretKeyRef`, nên một pod đang nạp run A vẫn
báo tiến độ được cho `run_id` của run B nếu nó biết id đó. Cách chữa là token
theo từng run (`loom-api` sinh một bí mật cho mỗi `ingest_run`, đặt vào Secret
riêng của Job đó, và kiểm theo `run_id` chứ không theo một hằng số toàn cụm) —
NỢ HOÃN LẠI có chủ ý, không xây ở 3a: nó thêm một vòng đời Secret phải tạo và
dọn cho mỗi lần nạp, trong khi ở 3a mọi pod nạp đều do chính control plane này
phóng ra từ cùng một image.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from loom_core.config import Settings
from loom_core.internal_auth import INGEST_SHARED_SECRET_HEADER


async def require_ingest_secret(request: Request) -> None:
    """401 nếu thiếu header hoặc giá trị sai — KHÔNG BAO GIỜ 403.

    403 nghĩa là "danh tính đã xác thực nhưng không đủ quyền"; ở đây chưa có
    danh tính nào được xác thực cả, nên 401 mới đúng ngữ nghĩa: "bạn chưa chứng
    minh được mình là cái gì được phép gọi route này".

    So bằng `hmac.compare_digest`, TUYỆT ĐỐI KHÔNG bằng `==`: `==` trên hai
    chuỗi so ký-tự-theo-ký-tự và THOÁT SỚM ngay ký tự đầu tiên khác nhau, nên
    thời gian trả lời tỉ lệ với SỐ KÝ TỰ ĐẦU khớp đúng chứ không phải hằng số.
    Kẻ tấn công đo thời gian phản hồi dò được TỪNG KÝ TỰ MỘT của bí mật — thử
    mọi ký tự ở vị trí 0, giữ lại ký tự làm request chậm hơn, rồi sang vị trí
    1 — thay vì phải đoán đúng cả chuỗi cùng lúc. `hmac.compare_digest` so
    trong thời gian không phụ thuộc nội dung, đóng đúng kênh rò rỉ đó.

    ĐỪNG "đơn giản hoá" dòng dưới về lại `==`. Bản sao của lời cảnh báo này đã
    có ở `loom_query.security.require_shared_secret` — hai bản chứ không một
    hàm dùng chung là CÓ CHỦ Ý: hai service đọc hai `Settings` khác nhau
    (`loom_core.config.Settings` với `loom_query.config.Settings`, xem docstring
    file sau), nên một hàm dùng chung sẽ phải nhận bí mật qua tham số và mất
    đúng thứ làm nó an toàn — việc nó tự lấy giá trị từ nguồn cấu hình của
    chính service, không phải từ tay người gọi.
    """
    settings: Settings = request.app.state.settings
    provided = request.headers.get(INGEST_SHARED_SECRET_HEADER)
    if provided is None or not hmac.compare_digest(provided, settings.ingest_shared_secret):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing or invalid internal shared secret"
        )
