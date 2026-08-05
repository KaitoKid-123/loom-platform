"""Phân quyền — HAI đường đánh giá, MỘT nguồn quy tắc.

Cả hai nửa nằm trong cùng file có chủ đích. `effective_role_for_*` trả lời cho
một tài nguyên; `visible_*_select` (Task 11) sinh câu truy vấn cho danh sách.
Chúng phải cho ra cùng kết quả, và một test đối chiếu ép điều đó. Nhưng test chỉ
bắt được KHI ĐÃ lệch — đặt cạnh nhau để người sửa một nửa nhìn thấy nửa kia ngay
dưới con trỏ là lớp phòng vệ rẻ nhất. Tách file là mời gọi drift.

Quy tắc nằm ở `loom_core.roles`, không nằm ở đây. File này chỉ dịch quy tắc đó
sang SQL.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.models import Item, RoleAssignment, Workspace
from loom_core.roles import Role, max_role
from loom_core.schemas import Principal

# Một CỘT SQL, không phải một giá trị. `Item.id` là InstrumentedAttribute còn
# cột của một subquery là ColumnElement, và SQLAlchemy không hứa hẹn tổ tiên
# chung nào cho hai họ đó, nên `Any` ở đây là mô tả trung thực chứ không phải
# một chỗ bỏ trống. Điều thật sự quan trọng — nhận cột chứ không nhận giá trị —
# được `_chain_conditions` nói rõ ở docstring của nó.
type ColumnLike = Any


class Forbidden(HTTPException):
    def __init__(self, detail: str = "không đủ quyền") -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, detail)


class NotVisible(HTTPException):
    """404 chứ không 403 khi principal không được ĐỌC tài nguyên. Trả 403 là tiết
    lộ sự tồn tại của nó, mà tên item thường mang thông tin
    (`acquisition_2026_finance`). Xem spec mục 4.5."""

    def __init__(self, detail: str = "không tìm thấy") -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, detail)


def principal_matches(principal: Principal) -> ColumnElement[bool]:
    """Điều kiện khớp principal: chính user đó, hoặc một nhóm người đó thuộc.

    Dùng CHUNG bởi cả hai đường — đây là chỗ dễ lệch nhất nếu viết hai lần.

    Không đọc `principal_type`: hàng cấp quyền được khớp theo CỘT nào có giá
    trị. `ck_role_assignment_principal_type` đã buộc hai thứ đó nhất quán ở tầng
    database, nên đọc thêm `principal_type` ở đây chỉ thêm một cách để lệch.
    """
    conditions: list[ColumnElement[bool]] = [RoleAssignment.principal_user_id == principal.user_id]
    if principal.groups:
        # Bỏ hẳn mệnh đề khi không có nhóm, thay vì `IN ()`: một danh sách rỗng
        # buộc SQLAlchemy sinh biểu thức luôn-sai kèm cảnh báo, và với người
        # không thuộc nhóm nào thì không có gì để hỏi.
        conditions.append(RoleAssignment.principal_group.in_(principal.groups))
    return or_(*conditions)


def _chain_conditions(
    item_id_col: ColumnLike | None,
    workspace_id_col: ColumnLike,
    domain_id_col: ColumnLike,
    tenant_id_col: ColumnLike,
) -> list[ColumnElement[bool]]:
    """Bốn nhánh của chuỗi tổ tiên. Bốn tầng CỐ ĐỊNH nên không cần recursive CTE.

    Cố định được vì `folder_path` là một chuỗi và thư mục KHÔNG phải thực thể —
    không có độ sâu tuỳ ý nào để đệ quy. Bốn nhánh vừa trong một điều kiện JOIN,
    nên một phép kiểm quyền là một round trip.

    Nhận CỘT chứ không nhận GIÁ TRỊ, để dùng được cho cả truy vấn một tài nguyên
    lẫn biểu thức lọc danh sách — cùng một hàm, cùng một logic. Viết chuỗi này
    hai lần là cách chắc chắn nhất để hai đường trôi khỏi nhau, và trôi ở đây
    nghĩa là người dùng thấy hàng không được phép thấy.

    `item_id_col=None` cho câu hỏi về workspace: chuỗi chỉ chạy TỪ tài nguyên
    LÊN. Một quyền trên item không phải là quyền trên workspace chứa nó.
    """
    branches = [
        and_(
            RoleAssignment.scope_type == "workspace",
            RoleAssignment.scope_id == workspace_id_col,
        ),
        # `domain_id` NULL-được. `scope_id = NULL` cho ra NULL chứ không phải
        # TRUE, nên workspace không thuộc domain nào tự nhiên không khớp nhánh
        # này — không cần vế IS NOT NULL riêng.
        and_(
            RoleAssignment.scope_type == "domain",
            RoleAssignment.scope_id == domain_id_col,
        ),
        and_(
            RoleAssignment.scope_type == "tenant",
            RoleAssignment.scope_id == tenant_id_col,
        ),
    ]
    if item_id_col is not None:
        branches.insert(
            0,
            and_(RoleAssignment.scope_type == "item", RoleAssignment.scope_id == item_id_col),
        )
    return branches


class PermissionService:
    """Một thực thể cho mỗi request. Cache để một trang 50 item không thành 50
    round trip.

    Cache nằm trên THỰC THỂ, không phải trên module: phạm vi của nó đúng bằng
    phạm vi của một request. Một `lru_cache` ở tầng module sẽ nhanh hơn nữa và
    có nghĩa là thu hồi quyền không có hiệu lực cho tới khi tiến trình khởi động
    lại.
    """

    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self._session = session
        self._principal = principal
        self._cache: dict[tuple[str, uuid.UUID], Role | None] = {}
        self.query_count = 0

    async def effective_role_for_item(self, item_id: uuid.UUID) -> Role | None:
        return await self._cached("item", item_id, self._query_item_roles)

    async def effective_role_for_workspace(self, workspace_id: uuid.UUID) -> Role | None:
        return await self._cached("workspace", workspace_id, self._query_workspace_roles)

    async def _cached(
        self,
        kind: str,
        key: uuid.UUID,
        query: Callable[[uuid.UUID], Awaitable[list[Role]]],
    ) -> Role | None:
        cache_key = (kind, key)
        if cache_key in self._cache:
            # `in` chứ không phải `.get() is not None`: None là một câu trả lời
            # hợp lệ và là câu trả lời PHỔ BIẾN NHẤT. Dùng giá trị để dò xem đã
            # cache chưa thì đúng những tài nguyên bị từ chối lại bị hỏi lại mỗi
            # lần — tức là cache mất tác dụng ở chính chỗ nó cần nhất.
            return self._cache[cache_key]
        self.query_count += 1
        role = max_role(await query(key))
        self._cache[cache_key] = role
        return role

    async def _query_item_roles(self, item_id: uuid.UUID) -> list[Role]:
        # MỘT truy vấn: join item→workspace để có domain_id, rồi khớp cả bốn
        # nhánh của chuỗi tổ tiên trong cùng điều kiện JOIN.
        #
        # Nhánh tenant dùng `Item.tenant_id` chứ không `Workspace.tenant_id`:
        # `item.tenant_id` không có FK (xem models.py), nên nếu hai cột từng
        # lệch nhau thì đọc theo item là hỏng KÍN — quyền tenant thôi không với
        # tới hàng đó nữa. Đọc theo workspace là hỏng MỞ.
        stmt = (
            select(RoleAssignment.role)
            .select_from(Item)
            .join(Workspace, Workspace.id == Item.workspace_id)
            .join(
                RoleAssignment,
                and_(
                    principal_matches(self._principal),
                    or_(
                        *_chain_conditions(
                            Item.id, Item.workspace_id, Workspace.domain_id, Item.tenant_id
                        )
                    ),
                ),
            )
            .where(Item.id == item_id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [Role[r] for r in rows]

    async def _query_workspace_roles(self, workspace_id: uuid.UUID) -> list[Role]:
        stmt = (
            select(RoleAssignment.role)
            .select_from(Workspace)
            .join(
                RoleAssignment,
                and_(
                    principal_matches(self._principal),
                    or_(
                        *_chain_conditions(
                            None, Workspace.id, Workspace.domain_id, Workspace.tenant_id
                        )
                    ),
                ),
            )
            .where(Workspace.id == workspace_id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [Role[r] for r in rows]
