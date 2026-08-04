"""Dependency dùng chung cho mọi router.

Giai đoạn 0 đọc cookie trong từng handler — chấp nhận được với bốn endpoint.
Giai đoạn 1 thêm khoảng mười lăm cái, và một endpoint sót xác thực là một
endpoint công khai. Một dependency, dùng ở mọi nơi, không có ngoại lệ.
"""

from collections.abc import AsyncIterator

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.db import Database
from loom_api.oidc_verifier import InvalidIdToken
from loom_api.user_store import UserStore
from loom_core.config import Settings
from loom_core.schemas import Principal

logger = structlog.get_logger(__name__)


async def get_principal(request: Request) -> Principal:
    # Chú thích kiểu tường minh, không để `app.state.X` chảy vào code dưới dạng
    # Any: state của Starlette trả Any cho mọi thuộc tính, nên không có mấy dòng
    # này thì mypy không kiểm được `load_session` có tồn tại hay trả về gì, và
    # `return principal` lặng lẽ thành "trả Any từ hàm khai báo trả Principal".
    settings: Settings = request.app.state.settings
    store: UserStore = request.app.state.user_store

    session_id = request.cookies.get(settings.session_cookie_name)
    if not session_id:
        # `not`, không phải `is None`: `Cookie: loom_session=` cho ra chuỗi rỗng,
        # và đi tra database bằng chuỗi rỗng là một round-trip vô ích trên mọi
        # request của khách chưa đăng nhập — một vector DoS rẻ.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "chưa đăng nhập")

    try:
        principal = await store.load_session(session_id)
    except InvalidIdToken as exc:
        # Hàng phiên không dựng lại được thành danh tính hợp lệ (subject rỗng do
        # dữ liệu hỏng). Từ Task 3, load_session() dịch ValidationError của
        # Principal thành đúng ngoại lệ này, nên nhánh dưới đây là mã SỐNG. Với
        # client thì phiên đơn giản là không dùng được — 401, không phải 500.
        logger.warning("auth.session_unusable", reason=exc.reason)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "phiên đã hết hạn") from exc

    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "phiên đã hết hạn")
    return principal


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Một AsyncSession cho mỗi request. Endpoint nào ghi thì tự commit — KHÔNG
    commit ngầm ở đây, vì audit phải nằm trong CÙNG transaction với thao tác nó
    mô tả (spec mục 5.4) và một commit tự động ở tầng dependency sẽ phá điều đó
    mà không ai thấy."""
    db: Database = request.app.state.db
    async with db.session() as session:
        yield session


PrincipalDep = Depends(get_principal)
SessionDep = Depends(get_db_session)
