"""Tìm kiếm cho ⌘K.

**CHỖ NÀY DỄ RÒ RỈ NHẤT trong cả API.** Mọi endpoint khác có `workspace_id` hoặc
`item_id` trong đường dẫn, nên người viết buộc phải nghĩ tới việc kiểm quyền trên
nó. Ở đây không có gì cả — một `select(Item).where(name.ilike(...))` trông hoàn
toàn hợp lý, chạy đúng, và trả về tên item của mọi workspace trong tổ chức. Tên
item mang thông tin (`acquisition-2026-finance`), và đó chính là lý do tài nguyên
không đọc được trả 404 chứ không 403.

Nên câu truy vấn nằm trong `search_items_select` chứ không viết thẳng trong
handler: `test_permissions_differential.py` gọi ĐÚNG hàm này. Một test dựng lại
biểu thức bằng tay chỉ chứng minh rằng bản dựng lại đó đúng.
"""

from fastapi import APIRouter, Query
from sqlalchemy import Select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.deps import PrincipalDep, SessionDep
from loom_api.models import Item
from loom_api.permissions import visible_items_select
from loom_core.schemas import Principal

router = APIRouter(tags=["search"])

MAX_RESULTS = 20


def _escape_like(term: str) -> str:
    """`%` và `_` là wildcard của LIKE, nên chúng phải thành ký tự thường.

    Không escape thì `q=%` trả về mọi item người dùng được đọc. Đó KHÔNG phải lỗ
    hổng quyền — bộ lọc quyền vẫn chạy — nhưng nó là một endpoint dump toàn bộ
    catalog mà không ai định làm ra, và `q=_` còn khớp mọi tên dài từ một ký tự.

    Dấu chéo ngược phải nhân đôi TRƯỚC hai phép thay còn lại, nếu không thì chính
    dấu chéo mình vừa thêm vào lại bị nhân đôi.
    """
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def search_items_select(principal: Principal, term: str) -> Select[tuple[Item]]:
    """Item khớp `term` VÀ người gọi được đọc.

    Nền là `visible_items_select` — cùng biểu thức mà endpoint danh sách dùng.
    Bộ lọc văn bản chỉ THU HẸP thêm, không bao giờ mở rộng: nó là một `.where()`
    nối vào, không phải một câu select mới.
    """
    pattern = f"%{_escape_like(term)}%"
    return (
        visible_items_select(principal)
        .where(
            or_(
                Item.name.ilike(pattern, escape="\\"),
                Item.display_name.ilike(pattern, escape="\\"),
            )
        )
        .order_by(Item.updated_at.desc(), Item.id.desc())
    )


@router.get("/search")
async def search(
    q: str = Query(default="", max_length=128),
    principal: Principal = PrincipalDep,
    session: AsyncSession = SessionDep,
) -> dict[str, object]:
    term = q.strip()
    if not term:
        # Query rỗng KHÔNG trả về mọi thứ. `%%` khớp mọi hàng, nên bỏ nhánh này là
        # biến ⌘K lúc mới mở thành một lệnh dump catalog.
        return {"items": []}

    rows = (
        (await session.execute(search_items_select(principal, term).limit(MAX_RESULTS)))
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(i.id),
                "workspace_id": str(i.workspace_id),
                "type": i.type,
                "name": i.name,
                "display_name": i.display_name,
                "folder_path": i.folder_path,
            }
            for i in rows
        ]
    }
