"""Canh `loom_connector` không có I/O ngoài nguồn — phép kiểm quan trọng nhất
của package này.

Đọc **AST** của từng module bằng `ast.parse`, KHÔNG import chúng rồi soi
`sys.modules`. Import kéo theo cả cây phụ thuộc gián tiếp — `psycopg` tự nó kéo
theo hàng chục module chuẩn — nên cách đó cho báo động giả và người ta sẽ nới
allowlist cho tới khi nó không canh gì nữa.

Vì sao ràng buộc này đáng có một phép kiểm riêng: `connectorkit` là ranh giới
giữa "đọc nguồn" và "ghi lakehouse". Nếu một connector cụ thể lẻn thêm một lệnh
gọi S3 hay Iceberg "cho tiện", ranh giới connector/task biến mất và mọi connector
sau này thừa hưởng luôn cái lỗ đó.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "loom_connector"

# KHÁC sqlkit ở hai chỗ, và cả hai là có chủ đích:
#   - `pyarrow` ĐƯỢC phép: `read()` trả `RecordBatch`, đó là cả điểm của package.
#   - `psycopg` ĐƯỢC phép: giao thức nguồn là lý do package này tồn tại.
# Thứ vẫn bị cấm là mọi thứ KHÁC chạm ra ngoài: HTTP, S3, catalog, k8s. Connector
# đọc nguồn và trả dữ liệu — nó không ghi Iceberg, không gọi loom-api, không biết
# lakehouse là gì.
ALLOWED = {
    "loom_connector",
    "pyarrow",
    "psycopg",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "itertools",
    "typing",
    "__future__",
}

WHY_BANNED = {
    "httpx": "gọi mạng — báo tiến độ là việc của loom-task, không phải connector",
    "requests": "gọi mạng",
    "boto3": "gọi S3 — connector đọc nguồn, không ghi storage",
    "pyiceberg": "chạm catalog — ghi bronze là việc của loom-task",
    "kubernetes": "chạm k8s",
    "sqlalchemy": "ORM — connector nói SQL thô với nguồn",
    "os": "chạm hệ tệp và môi trường — cấu hình phải đi qua tham số khởi tạo",
    "subprocess": "chạy tiến trình ngoài",
    "logging": "connector không tự log; nó trả dữ liệu và ném lỗi",
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
        if name != "loom_connector" and name not in ALLOWED
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
