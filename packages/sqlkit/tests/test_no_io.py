"""Canh `loom_sql` không có I/O — phép kiểm quan trọng nhất của package này.

Đọc **AST** của từng module bằng `ast.parse`, KHÔNG import chúng rồi soi
`sys.modules`. Import kéo theo cả cây phụ thuộc gián tiếp — `sqlglot` tự nó kéo
theo hàng chục module chuẩn — nên cách đó cho báo động giả và người ta sẽ nới
allowlist cho tới khi nó không canh gì nữa.

Vì sao ràng buộc này đáng có một phép kiểm riêng: `sqlkit` là chỗ RBAC gặp SQL.
`table_deps` quyết định bảng nào bị kiểm quyền, nên nó phải test được cho MỌI
trường hợp — CTE, subquery, union, alias — chứ không chỉ những trường hợp dựng
nổi một database. Một lần `import httpx` lọt vào là bắt đầu đường trượt tới chỗ
phải có mạng mới test được.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "loom_sql"

# Thư viện chuẩn thuần tính toán, cộng đúng một phụ thuộc ngoài.
ALLOWED = {
    "sqlglot",
    "dataclasses",
    "enum",
    "typing",
    "re",
    "collections",
    "functools",
    "itertools",
    "__future__",
}

# Không cần thiết cho việc canh — allowlist đã chặn tất cả. Danh sách này tồn tại
# để THÔNG BÁO LỖI nói được vì sao, thay vì chỉ nói "không nằm trong allowlist".
WHY_BANNED = {
    "httpx": "gọi mạng",
    "requests": "gọi mạng",
    "urllib": "gọi mạng",
    "socket": "gọi mạng",
    "boto3": "gọi S3",
    "botocore": "gọi S3",
    "sqlalchemy": "chạm database",
    "asyncpg": "chạm database",
    "psycopg2": "chạm database",
    "duckdb": "chạm engine — sqlkit chỉ đọc AST, không chạy SQL",
    "pyiceberg": "chạm catalog",
    "pyarrow": "kéo theo cả tầng I/O của Arrow",
    "pathlib": "chạm hệ tệp",
    "os": "chạm hệ tệp và môi trường",
    "io": "chạm hệ tệp",
    "subprocess": "chạy tiến trình ngoài",
}


def _modules() -> list[Path]:
    found = sorted(SRC.glob("*.py"))
    assert found, f"không thấy module nào trong {SRC} — phép kiểm này sẽ xanh oan"
    return found


def _imports(path: Path) -> list[tuple[str, int]]:
    """(tên gốc của module được import, số dòng) cho mọi câu import trong file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name.split(".")[0], node.lineno) for alias in node.names)
        # `from . import x` có module=None và level>0 — import nội bộ, luôn hợp lệ.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module.split(".")[0], node.lineno))
    return out


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_module_imports_nothing_that_touches_the_outside_world(module: Path) -> None:
    offenders = [
        (name, line)
        for name, line in _imports(module)
        if name != "loom_sql" and name not in ALLOWED
    ]
    assert not offenders, "\n".join(
        f"{module.name}:{line} import `{name}`"
        + (f" — {WHY_BANNED[name]}" if name in WHY_BANNED else " — ngoài allowlist")
        for name, line in offenders
    )


def test_the_guard_can_see_a_violation(tmp_path: Path) -> None:
    """Phép canh cho chính phép canh.

    `_imports` là thứ duy nhất đứng giữa một `import httpx` và việc nó lọt qua.
    Nếu nó bỏ sót một DẠNG câu import — `from x import y`, import trong hàm,
    import lồng trong `if TYPE_CHECKING` — thì phép trên xanh mà không canh gì.

    Kiểm bằng mã giả lập chứa đủ bốn dạng, thay vì tin rằng `ast.walk` bắt hết.
    """
    source = (
        "import httpx\n"
        "from boto3 import client\n"
        "import sqlglot\n"
        "def f():\n"
        "    import socket\n"
        "if True:\n"
        "    from duckdb import connect\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")

    assert {name for name, _ in _imports(probe)} == {
        "httpx",
        "boto3",
        "sqlglot",
        "socket",
        "duckdb",
    }
