import ast
import itertools
from pathlib import Path

import pytest

import loom_core.roles
from loom_core.roles import (
    ACTION_MATRIX,
    GRANTABLE_BY,
    SCOPE_CHAIN,
    Action,
    Role,
    allows,
    max_role,
)


def test_role_ordering() -> None:
    assert Role.viewer < Role.contributor < Role.member < Role.admin


def test_max_role_picks_highest() -> None:
    """Chuỗi rỗng phải trả `None`, KHÔNG phải `Role.viewer`. Chỗ gọi biến `None`
    thành 404 (không được biết tài nguyên có tồn tại) còn `viewer` thì cho đọc —
    nên `default=Role.viewer` là một lỗ hổng chứ không phải một mặc định tiện tay.
    `is None` ở đây tự tay giết được đúng đột biến đó."""
    assert max_role([Role.viewer, Role.member, Role.contributor]) is Role.member
    assert max_role([Role.viewer, Role.admin]) is Role.admin
    assert max_role([Role.contributor]) is Role.contributor
    assert max_role([]) is None


@pytest.mark.parametrize(
    ("role", "action", "expected"),
    [
        (Role.viewer, Action.item_read, True),
        (Role.viewer, Action.item_create, False),
        (Role.contributor, Action.item_create, True),
        (Role.contributor, Action.workspace_update, False),
        (Role.member, Action.workspace_update, True),
        (Role.member, Action.workspace_delete, False),
        (Role.admin, Action.workspace_delete, True),
    ],
)
def test_action_matrix(role: Role, action: Action, expected: bool) -> None:
    assert allows(role, action) is expected


def test_member_cannot_grant_member_or_admin() -> None:
    """Quy tắc chống leo thang quyền thứ nhất, spec mục 4.4. Thiếu nó thì bất kỳ
    member nào cũng tự nâng mình thành admin trong một bước."""
    assert GRANTABLE_BY[Role.member] == frozenset({Role.viewer, Role.contributor})
    assert Role.admin not in GRANTABLE_BY[Role.member]
    assert Role.member not in GRANTABLE_BY[Role.member]
    assert GRANTABLE_BY[Role.admin] == frozenset(Role)


def test_scope_chain_is_fixed_depth_and_ordered_narrow_to_wide() -> None:
    """Bốn tầng cố định là lý do không cần recursive CTE. Thứ tự từ hẹp tới rộng
    để đọc code phân quyền theo đúng chiều tìm kiếm."""
    assert SCOPE_CHAIN == ("item", "workspace", "domain", "tenant")


def test_every_action_appears_in_matrix() -> None:
    """Thêm Action mới mà quên khai quyền cho nó là lỗi mở cửa: `allows()` trả
    False cho mọi vai trò, kể cả admin, và endpoint chết một cách khó hiểu."""
    for action in Action:
        assert any(action in ACTION_MATRIX[r] for r in Role), action


# --------------------------------------------------------------------------
# Những test dưới đây bịt các lỗ mà mutation testing tìm ra: bộ test ở trên
# chỉ khẳng định 7 trong 44 ô của bảng, nên nhiều đột biến NỚI quyền sống sót
# (viewer nhận được role.grant, item.delete, domain.manage... mà không test nào
# đỏ). Với một module tự nhận là "nguồn quy tắc duy nhất" thì bảng phải được
# viết ra nguyên vẹn một lần nữa ở đây — đó mới là spec, chứ không phải mẫu.
# --------------------------------------------------------------------------

# Cố ý liệt kê thẳng, KHÔNG dùng lại `_VIEWER`/`_CONTRIBUTOR`... của roles.py:
# đọc lại chính biến đang kiểm tra thì test chỉ còn là phép đồng nhất.
_EXPECTED_MATRIX: dict[Role, frozenset[Action]] = {
    Role.viewer: frozenset(
        {
            Action.workspace_read,
            Action.item_read,
        }
    ),
    Role.contributor: frozenset(
        {
            Action.workspace_read,
            Action.item_read,
            Action.item_create,
            Action.item_update,
            Action.item_delete,
        }
    ),
    Role.member: frozenset(
        {
            Action.workspace_read,
            Action.item_read,
            Action.item_create,
            Action.item_update,
            Action.item_delete,
            Action.workspace_update,
            Action.role_read,
            Action.role_grant,
            Action.audit_read,
        }
    ),
    Role.admin: frozenset(
        {
            Action.workspace_read,
            Action.item_read,
            Action.item_create,
            Action.item_update,
            Action.item_delete,
            Action.workspace_update,
            Action.role_read,
            Action.role_grant,
            Action.audit_read,
            Action.workspace_delete,
            Action.domain_manage,
        }
    ),
}


@pytest.mark.parametrize("role", list(Role))
def test_action_matrix_row_is_exactly_this(role: Role) -> None:
    """Mọi ô của bảng, không chỉ 7 ô mẫu. Nới quyền cho một vai trò là thay đổi
    duy nhất nguy hiểm nhất có thể xảy ra trong file này và nó phải không lọt
    được qua đây."""
    assert ACTION_MATRIX[role] == _EXPECTED_MATRIX[role]


def test_action_matrix_covers_every_role_and_nothing_else() -> None:
    """`allows()` index thẳng vào ACTION_MATRIX[role]; thiếu một hàng là KeyError
    giữa request, không phải một lần từ chối gọn gàng."""
    assert set(ACTION_MATRIX) == set(Role)


def test_allows_agrees_with_the_table_for_every_pair() -> None:
    """44 cặp, không chừa cặp nào cho suy diễn."""
    for role in Role:
        for action in Action:
            assert allows(role, action) is (action in _EXPECTED_MATRIX[role]), (role, action)


def test_higher_role_never_loses_a_permission() -> None:
    """Vai trò hiệu lực là `max_role()` trên chuỗi tổ tiên. Nếu vai trò cao hơn
    thiếu một quyền mà vai trò thấp có, thì trèo lên một scope rộng hơn lại LÀM
    MẤT quyền — và max() im lặng chọn đúng cái sai đó."""
    for lower, higher in itertools.pairwise(sorted(Role)):
        assert ACTION_MATRIX[lower] <= ACTION_MATRIX[higher], (lower, higher)


_EXPECTED_GRANTABLE: dict[Role, frozenset[Role]] = {
    Role.viewer: frozenset(),
    Role.contributor: frozenset(),
    Role.member: frozenset({Role.viewer, Role.contributor}),
    Role.admin: frozenset({Role.viewer, Role.contributor, Role.member, Role.admin}),
}


def test_grantable_by_is_exactly_this() -> None:
    """`test_member_cannot_grant_member_or_admin` chỉ khẳng định 2 trong 4 hàng.
    Hàng viewer và contributor bỏ trống thì `GRANTABLE_BY[Role.viewer] =
    frozenset(Role)` lọt qua toàn bộ bộ test — cộng với một đột biến nới
    ACTION_MATRIX cũng lọt được, hai cái ghép lại thành viewer tự lên admin."""
    assert GRANTABLE_BY == _EXPECTED_GRANTABLE


def test_nobody_can_grant_above_their_own_role() -> None:
    """Bất biến tổng quát của quy tắc chống leo thang, phát biểu độc lập với bảng
    ở trên: không vai trò nào gán được vai trò cao hơn chính nó."""
    for role, grantable in GRANTABLE_BY.items():
        assert all(target <= role for target in grantable), (role, grantable)


def test_only_roles_holding_role_grant_can_grant_anything() -> None:
    """Hai bảng phải khớp nhau: có tên trong GRANTABLE_BY mà không có quyền
    `role.grant` là quy tắc chết, còn ngược lại là quyền không giới hạn."""
    for role in Role:
        assert bool(GRANTABLE_BY[role]) is allows(role, Action.role_grant), role


def test_role_str_is_the_name_because_the_db_column_is_text() -> None:
    """Cột `role` là `String(16)` và chỗ ghi dùng `role=str(role)`. `Role` là
    IntEnum, nên nếu `__str__` biến mất thì `str(Role.viewer)` trả "10": vừa khít
    16 ký tự, qua được kiểu cột, và chỉ bị CheckConstraint
    `role IN ('viewer',...)` chặn — tức là mọi lần gán quyền đổ 500 ở runtime.
    Ghim ở đây để lỗi đó là một unit test đỏ chứ không phải một sự cố."""
    for role in Role:
        assert str(role) == role.name
        assert Role[str(role)] is role
    assert str(Role.viewer) == "viewer"


def test_action_values_are_the_dotted_wire_format() -> None:
    """`Action` là StrEnum để giá trị của nó ĐI RA NGOÀI: audit log, payload API,
    biểu thức SQL. Đổi một giá trị là đổi giao diện công khai, nên nó được ghim
    ở đây thay vì suy ra từ tên thành viên."""
    assert {a.name: a.value for a in Action} == {
        "workspace_read": "workspace.read",
        "workspace_update": "workspace.update",
        "workspace_delete": "workspace.delete",
        "item_read": "item.read",
        "item_create": "item.create",
        "item_update": "item.update",
        "item_delete": "item.delete",
        "role_read": "role.read",
        "role_grant": "role.grant",
        "audit_read": "audit.read",
        "domain_manage": "domain.manage",
    }


# Chỉ hai module stdlib này. Danh sách theo kiểu allowlist nên nó ĐÓNG: mọi
# import mới đều đỏ, kể cả `loom_api.db`, `.storage` hay `importlib` (đường lách
# duy nhất khỏi một phép kiểm tra AST).
_ALLOWED_IMPORT_ROOTS = frozenset({"collections", "enum"})


def test_roles_module_imports_nothing_outside_the_allowlist() -> None:
    """Lý do tồn tại của module này là nó KHÔNG biết gì về tầng lưu trữ: hai
    đường đánh giá quyền sắp tới (Python cho một tài nguyên, biểu thức SQL cho
    endpoint danh sách) phải đọc chung đúng bộ dữ liệu này, và cách buộc được
    điều đó là file này không thể mọc ra một câu truy vấn.

    Không có test này thì `import sqlalchemy` vào roles.py vẫn xanh cả bộ — đã
    kiểm tra bằng cách thêm thật rồi xoá. Một quy ước không ai kiểm là một quy
    ước sẽ mất."""
    source = Path(loom_core.roles.__file__).read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 là import tương đối, tức là trỏ vào chính loom_core —
            # nơi tầng lưu trữ sẽ sống. Giữ nguyên dấu chấm để thông báo lỗi đọc được.
            prefix = "." * node.level
            found.add(prefix + (node.module or "").split(".")[0])
    offenders = found - _ALLOWED_IMPORT_ROOTS
    assert not offenders, (
        f"roles.py import {sorted(offenders)}. Module này phải ở dạng dữ liệu thuần: "
        f"không SQLAlchemy, không tầng lưu trữ, không import động. Nếu import mới thật sự "
        f"vô can với lưu trữ thì thêm vào _ALLOWED_IMPORT_ROOTS một cách có ý thức."
    )
