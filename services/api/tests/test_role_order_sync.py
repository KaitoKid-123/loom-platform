"""Thứ tự vai trò ở frontend phải khớp `Role` ở backend.

`web/src/lib/useWorkspaces.ts` giữ một BẢN SAO của thứ tự vai trò, và nó phải có:
frontend cần so sánh vai trò để ẩn nút mà server sẽ từ chối, còn `Role` là một
`IntEnum` Python thì trình duyệt không đọc được.

Bản sao thì trôi, và trôi ở đây nghĩa là giao diện hiện nút mà server từ chối (người
dùng bấm rồi ăn 403) hoặc ẩn nút mà server cho phép (tính năng biến mất không dấu
vết). Cả hai đều im lặng — không có exception, không có dòng log nào.

Nên phép kiểm này đọc CHÍNH file TypeScript đó. Cùng khuôn với test AST canh
`roles.py` không import SQLAlchemy: biến một lời nhắc trong comment thành một ràng
buộc mà build kiểm được.
"""

import re
from pathlib import Path

import pytest

from loom_core.roles import Role

_TS = Path(__file__).resolve().parents[3] / "web" / "src" / "lib" / "useWorkspaces.ts"
_ROLE_ORDER = re.compile(r"const ROLE_ORDER = \[([^\]]*)\] as const", re.S)


def _frontend_role_order() -> list[str]:
    source = _TS.read_text(encoding="utf-8")
    match = _ROLE_ORDER.search(source)
    # Không tìm thấy là ĐỎ, không phải bỏ qua: nếu ai đó đổi tên hằng số hoặc dời nó
    # sang file khác thì phép kiểm này phải hỏng ầm ĩ, chứ không âm thầm thành vô dụng
    # — đó đúng là kiểu "phép kiểm xanh mà không kiểm gì" mà dự án này đã gặp nhiều lần.
    assert match, f"không tìm thấy `const ROLE_ORDER = [...] as const` trong {_TS}"
    return re.findall(r"'([^']+)'", match.group(1))


def test_the_typescript_file_exists_where_this_test_looks() -> None:
    """Đường dẫn được tính từ vị trí file test. Sai đường dẫn thì mọi phép kiểm dưới
    đây ném `FileNotFoundError` — vẫn đỏ, nhưng đỏ với lý do gây hoang mang."""
    assert _TS.is_file(), f"không có {_TS}"


def test_frontend_role_order_matches_the_backend_exactly() -> None:
    # Đẳng thức trên cả DANH SÁCH, không phải trên tập hợp: thứ tự CHÍNH LÀ ngữ nghĩa
    # ở đây — `atLeast` so sánh bằng chỉ số, nên đảo hai phần tử là đảo cả cây quyền
    # trong khi tập hợp vẫn bằng nhau.
    assert _frontend_role_order() == [str(role) for role in Role]


@pytest.mark.parametrize("role", list(Role))
def test_every_backend_role_is_known_to_the_frontend(role: Role) -> None:
    """Tham số hoá để thông báo lỗi nói ĐÚNG vai trò nào bị thiếu, thay vì chỉ nói hai
    danh sách khác nhau."""
    assert str(role) in _frontend_role_order()
