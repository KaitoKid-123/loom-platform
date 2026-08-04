"""Quy tắc phân quyền dạng dữ liệu thuần — NGUỒN DUY NHẤT.

File này CỐ Ý không import SQLAlchemy hay bất cứ thứ gì thuộc tầng lưu trữ.
`permissions.py` có hai đường đánh giá (Python cho một tài nguyên, biểu thức SQL
cho danh sách) và cả hai phải đọc đúng cùng bộ quy tắc ở đây. Nếu file này biết
về SQLAlchemy thì sẽ có ngày ai đó nhúng logic vào một bên và bên kia lệch đi —
mà lệch ở đây nghĩa là người dùng thấy dữ liệu không được phép thấy.

`test_roles.py` khoá điều đó lại bằng một test đọc AST của chính file này và bác
mọi import ngoài allowlist, nên "cố ý" ở trên là một điều kiện được kiểm tra chứ
không phải một lời nhắc.
"""

from collections.abc import Iterable
from enum import IntEnum, StrEnum


class Role(IntEnum):
    """IntEnum để so sánh được bằng < và dùng max() trực tiếp. Giá trị số KHÔNG
    lưu vào database — cột `role` là text, xem `Role.__str__`."""

    viewer = 10
    contributor = 20
    member = 30
    admin = 40

    def __str__(self) -> str:
        return self.name


class Action(StrEnum):
    workspace_read = "workspace.read"
    workspace_update = "workspace.update"
    workspace_delete = "workspace.delete"
    item_read = "item.read"
    item_create = "item.create"
    item_update = "item.update"
    item_delete = "item.delete"
    role_read = "role.read"
    role_grant = "role.grant"
    audit_read = "audit.read"
    domain_manage = "domain.manage"


# Từ hẹp tới rộng. Bốn tầng CỐ ĐỊNH — đây là lý do không cần recursive CTE.
SCOPE_CHAIN: tuple[str, ...] = ("item", "workspace", "domain", "tenant")

_VIEWER = frozenset({Action.workspace_read, Action.item_read})
_CONTRIBUTOR = _VIEWER | {Action.item_create, Action.item_update, Action.item_delete}
_MEMBER = _CONTRIBUTOR | {
    Action.workspace_update,
    Action.role_read,
    Action.role_grant,
    Action.audit_read,
}
_ADMIN = _MEMBER | {Action.workspace_delete, Action.domain_manage}

ACTION_MATRIX: dict[Role, frozenset[Action]] = {
    Role.viewer: _VIEWER,
    Role.contributor: _CONTRIBUTOR,
    Role.member: _MEMBER,
    Role.admin: _ADMIN,
}

# Quy tắc chống leo thang quyền thứ nhất (spec mục 4.4): member gán được tới
# contributor, KHÔNG gán được member hay admin. Không có dòng này thì `role.grant`
# một mình cho phép member tự nâng thành admin.
GRANTABLE_BY: dict[Role, frozenset[Role]] = {
    Role.viewer: frozenset(),
    Role.contributor: frozenset(),
    Role.member: frozenset({Role.viewer, Role.contributor}),
    Role.admin: frozenset(Role),
}


def allows(role: Role, action: Action) -> bool:
    return action in ACTION_MATRIX[role]


def max_role(roles: Iterable[Role]) -> Role | None:
    """Vai trò hiệu lực là max() trên chuỗi tổ tiên. None nghĩa là không có
    quyền gì — khác hẳn với viewer, và chỗ gọi phải phân biệt được."""
    return max(roles, default=None)
