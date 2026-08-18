"""Hình dạng của `GET /api/v1/me` — và cái giá mà thân hàm nó KHÔNG được cộng thêm.

Ba phép canh, ba điều khác nhau:

1. `user_id` có mặt. Không có nó, giao diện không bật được lịch:
   `ScheduleDefinition` bắt buộc `run_as_user_id` là uuid của bảng `user`, và
   `/me` là chỗ duy nhất UI biết id của chính người đang đăng nhập.
2. Handler KHÔNG nhận một tham số session/`AsyncSession`. `/me` được gọi mỗi lần
   tải trang, nên một round trip THÊM ở đây — CỘNG vào chi phí xác thực mà
   `PrincipalDep` đã trả trước khi thân hàm chạy — là một round trip nữa cho MỌI
   trang.
3. Thân hàm `me` không tự truy cập `.state`/`.user_store`/`.execute`/... — tức
   không có đường nào trong CHÍNH MÃ của `me` gọi thẳng vào store hay session,
   kể cả khi không đi qua tham số nào cả.

Phép canh (2) đọc CHỮ KÝ HÀM, không đếm câu lệnh SQL — và đó là một quyết định
có lý do. Fixture `api_world` dựng app bằng `Database(db_engine.url...)`, tức app
mở một engine MỚI; listener của fixture `sql_log` gắn trên engine của TEST. Một
`assert sql_log[mark:] == []` quanh một request đi qua `api_world.client` vì vậy
XANH kể cả khi handler truy vấn database — nó rỗng nghĩa.

NHƯNG phép canh (2) cũng không thấy HẾT — nó chỉ bắt được một handler NHẬN
session qua THAM SỐ (kiểu `SessionDep`/`AsyncSession`). `login`, `callback` và
`logout` trong CÙNG FILE này chạm database qua một đường khác hẳn:
`request.app.state.user_store...` — không tham số nào của chúng tên hay kiểu là
"session" cả. Một `me(request: Request, ...)` gọi
`request.app.state.user_store.load_session(...)` trong thân hàm sẽ đi lọt qua
phép canh (2) mà vẫn chạm database thật. Đó là lý do phép canh (3) tồn tại: nó
đọc AST của THÂN HÀM `me`, không phải chữ ký, và bắt đúng dạng
`request.app.state...` mà (2) không thấy. (3) vẫn có một khoảng trống thừa
nhận, không phải phép canh toàn năng: một HÀM PHỤ được `me` gọi mà tự mở
session/query riêng sẽ đi lọt qua CẢ (3), vì AST này không theo dấu lệnh gọi
hàm — xem chú thích tại `BANNED_ATTRS_IN_ME_BODY`.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

from loom_api.routers.auth import me
from loom_core.schemas import CurrentUser, Principal

AUTH_ROUTER = Path(__file__).resolve().parents[1] / "src" / "loom_api" / "routers" / "auth.py"

# Canh THÂN HÀM `me` bằng AST, không phải chữ ký — xem docstring module về khoảng
# trống mà phép canh chữ ký (`test_me_does_not_take_a_database_session`) không
# thấy. Mỗi tên trong danh sách, và vì sao nó ở đây:
#   - "state": `login`/`callback`/`logout` trong CÙNG FILE này chạm database qua
#     đúng `request.app.state.user_store...` — cấm truy cập `.state` chặn đúng
#     đường đó, dù tham số được đặt tên là gì.
#   - "user_store": phòng khi `request.app.state.user_store` bị gán ra một biến
#     cục bộ rồi gọi qua biến đó — cấm riêng `.state` sẽ không thấy `.user_store`
#     nếu code đọc nó gián tiếp qua một tên khác.
#   - "execute", "scalar", "scalar_one_or_none", "commit": tên phương thức
#     `AsyncSession` (SQLAlchemy) hay dùng nhất để chạm database trực tiếp —
#     phòng khi `me` nhận một session bằng một con đường khác `SessionDep` (chữ
#     ký đã bắt riêng `SessionDep`/kiểu `Session` ở phép canh (2)) rồi gọi thẳng
#     một trong các phương thức này.
# CÁI GIỚI HẠN THẬT của danh sách này, nói thẳng: nó chỉ đọc AST của thân `me`,
# KHÔNG theo dấu lệnh gọi hàm. Một hàm phụ (vd. `_load_something()`) được `me`
# gọi mà bên trong nó tự mở session và query riêng sẽ KHÔNG bị bắt — theo dấu
# lệnh gọi hàm (interprocedural) nằm ngoài phạm vi phép canh này.
BANNED_ATTRS_IN_ME_BODY = {
    "state",
    "user_store",
    "execute",
    "scalar",
    "scalar_one_or_none",
    "commit",
}


def _me_function_node(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "me":
            return node
    raise AssertionError("không tìm thấy hàm async `me` trong cây AST")


def _attribute_accesses_in_body(func: ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    """(tên thuộc tính, số dòng) cho mọi truy cập thuộc tính trong THÂN hàm — cố ý
    KHÔNG tính decorator hay chữ ký (tham số/annotation/giá trị mặc định): những
    chỗ đó không chạy khi handler được gọi, thân hàm mới là thứ thật sự thực thi.
    """
    found: list[tuple[str, int]] = []
    for stmt in func.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Attribute):
                found.append((node.attr, node.lineno))
    return found


def test_current_user_carries_user_id() -> None:
    fields = CurrentUser.model_fields
    assert "user_id" in fields, (
        "CurrentUser phải mang user_id — không có nó thì UI không đặt được run_as_user_id"
    )
    assert fields["user_id"].annotation is uuid.UUID


async def test_me_returns_the_principals_user_id() -> None:
    user_id = uuid.uuid4()
    principal = Principal(
        user_id=user_id,
        subject="alice",
        email="alice@loom.local",
        display_name="Alice",
        groups=("authors",),
    )

    result = await me(principal=principal)

    assert result.user_id == user_id
    # Các trường cũ KHÔNG được mất khi thêm trường mới.
    assert result.subject == "alice"
    assert result.email == "alice@loom.local"
    assert result.display_name == "Alice"
    assert result.groups == ("authors",)


def test_me_does_not_take_a_database_session() -> None:
    """`/me` gọi mỗi lần tải trang — một session ở đây là một round trip THÊM cho
    mọi trang.

    Đây là phép canh THAM SỐ, không phải phép canh "không chạm database" toàn
    phần — nó chỉ bắt được một handler NHẬN session qua tham số. Phép canh AST
    `test_me_body_has_no_database_access_by_ast` bên dưới bắt dạng khác: chạm
    database qua `request.app.state...` mà không tham số nào tên/kiểu là
    session. Xem docstring module về vì sao cả hai đều cần, và vì sao ngay cả
    hai cái gộp lại vẫn không phải toàn năng.
    """
    signature = inspect.signature(me)
    names = set(signature.parameters)
    assert "session" not in names, (
        f"`me` nhận tham số {sorted(names)} — một session ở đây là một truy vấn "
        "database THÊM cho MỌI lần tải trang"
    )
    annotations = [str(p.annotation) for p in signature.parameters.values()]
    assert not any("Session" in a for a in annotations), (
        f"`me` nhận một kiểu session: {annotations}"
    )


def test_me_body_has_no_database_access_by_ast() -> None:
    """Phép canh (3) — xem docstring module. Đọc AST THÂN HÀM `me` trong chính
    file nguồn `auth.py`, KHÔNG import module (cùng khuôn
    `test_root_credential_guard.py` / `test_no_db_no_k8s.py`): import kéo theo
    cả cây phụ thuộc, còn ast.parse chỉ thấy đúng những gì thân hàm THẬT SỰ viết
    ra. Bắt đúng dạng mà phép canh chữ ký ở trên không thấy: chạm database qua
    `request.app.state...` thay vì qua một tham số session.
    """
    tree = ast.parse(AUTH_ROUTER.read_text(encoding="utf-8"), filename=str(AUTH_ROUTER))
    offenders = [
        (attr, line)
        for attr, line in _attribute_accesses_in_body(_me_function_node(tree))
        if attr in BANNED_ATTRS_IN_ME_BODY
    ]
    assert not offenders, (
        "thân hàm `me` truy cập "
        + ", ".join(f"`.{attr}` (dòng {line})" for attr, line in offenders)
        + " — đây là dạng chạm database mà phép canh chữ ký không thấy được"
    )


def test_the_ast_guard_can_see_a_violation(tmp_path: Path) -> None:
    """Phép canh cho chính phép canh (3): `_attribute_accesses_in_body` phải
    thấy đúng dạng vi phạm mà nó được viết ra để bắt — `request.app.state.
    user_store...` trong thân một hàm async tên `me` — chứ không chỉ xanh vì
    không có gì để tìm."""
    source = (
        "async def me(request):\n"
        "    return await request.app.state.user_store.load_session('probe')\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    tree = ast.parse(probe.read_text(encoding="utf-8"), filename=str(probe))

    found = {attr for attr, _ in _attribute_accesses_in_body(_me_function_node(tree))}
    assert found & BANNED_ATTRS_IN_ME_BODY == {"state", "user_store"}
