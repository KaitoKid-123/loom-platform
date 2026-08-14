"""Bí mật chia sẻ giữa `loom-api` và `loom-query`, qua header (Task 10/11).

Xem docstring `loom_core.internal_auth` cho LÝ DO tồn tại: `loom-query` nhận
principal của người dùng cuối ngay trong thân request (`schemas.QueryCreate.
principal`) và KHÔNG tự xác thực ai — nó tin nguyên request. ClusterIP chỉ
chặn traffic từ NGOÀI cluster; bất kỳ pod nào khác trong cùng namespace vẫn
POST thẳng được và tự xưng là bất kỳ ai, kể cả tenant admin. Header này chứng
minh request tới TỪ `loom-api` — nơi DUY NHẤT biết bí mật.

Áp dụng cho CẢ BA route (`POST`/`GET`/`DELETE` của `routers/query.py`) qua
`dependencies=` ở cấp `APIRouter`, không phải lặp lại `Depends(...)` ở từng
handler — ba route đều cần đúng MỘT phép kiểm này, và một handler mới thêm
sau này tự động được bọc mà không ai phải nhớ dán dependency vào nó.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from loom_core.internal_auth import QUERY_SHARED_SECRET_HEADER
from loom_query.config import Settings


async def require_shared_secret(request: Request) -> None:
    """401 nếu thiếu header hoặc giá trị sai — KHÔNG BAO GIỜ 403.

    403 nghĩa là "danh tính đã xác thực nhưng không đủ quyền"; ở đây chưa có
    danh tính nào được xác thực cả, nên 401 (`WWW-Authenticate` không áp dụng
    vì đây không phải xác thực người dùng, nhưng ngữ nghĩa mã trạng thái vẫn
    đúng: "bạn chưa chứng minh được bạn là ai/cái gì được phép gọi route này").

    So bằng `hmac.compare_digest`, TUYỆT ĐỐI KHÔNG bằng `==`: so hai chuỗi
    bằng `==` là so ký-tự-theo-ký-tự và THOÁT SỚM ngay ký tự đầu tiên khác
    nhau, nên thời gian trả lời tỉ lệ thuận với SỐ KÝ TỰ ĐẦU khớp đúng, không
    phải hằng số. Một kẻ tấn công đo thời gian phản hồi (timing attack) dò
    được TỪNG KÝ TỰ MỘT của bí mật — thử mọi ký tự ở vị trí 0, giữ ký tự làm
    request chậm hơn một chút, rồi sang vị trí 1 — thay vì phải đoán đúng cả
    chuỗi cùng một lúc. `hmac.compare_digest` so trong thời gian KHÔNG phụ
    thuộc nội dung hai chuỗi (constant-time), đóng đúng kênh rò rỉ đó.

    ĐỪNG "đơn giản hoá" dòng so sánh dưới đây về lại `==` — trông vô hại (cả
    hai đều là closures kiểm bằng có khớp hay không) nhưng chỉ MỘT tráo đổi
    này thôi là mở lại đúng lỗ timing attack mà dòng comment này tồn tại để
    chặn người tới sau lặp lại.

    **So trên BYTES, không trên `str`** (sửa ở Giai đoạn 3a, Task 10, khi lỗi
    này lộ ra lúc dựng bản tương đương cho `loom-api`):
    `hmac.compare_digest` trên hai `str` NÉM `TypeError: comparing strings with
    non-ASCII characters is not supported`. Starlette giải mã header bằng
    latin-1, nên MỘT byte >127 trong header là đủ để cổng này trả 500 kèm
    nguyên traceback thay vì 401 — một request không cần xác thực gì mà ép được
    server 500. Xem `loom_api.internal_security.require_ingest_secret` cho lý
    do cặp `latin-1`/`utf-8` là đúng chứ không tuỳ tiện.
    """
    settings: Settings = request.app.state.settings
    provided = request.headers.get(QUERY_SHARED_SECRET_HEADER)
    if provided is None or not hmac.compare_digest(
        provided.encode("latin-1", "replace"), settings.shared_secret.encode("utf-8")
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing or invalid internal shared secret"
        )
