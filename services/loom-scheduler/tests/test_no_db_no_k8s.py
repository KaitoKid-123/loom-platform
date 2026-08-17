"""Canh `loom-scheduler` là CÁI ĐỒNG HỒ, không phải bộ não.

Hai tính chất, và cả hai là lý do service này tồn tại ở dạng hiện tại:

* **Không database.** Nó tốn 0 connection slot trên Aiven — ngân sách là 20
  slot, trạng thái ổn định của Loom đã dùng ~7 (loom-api 3+2, Lakekeeper 3+2),
  và ứng dụng khác của chủ dự án đã có lần lấp kín phần còn lại. Một pool ở đây
  là một pool phải chia phần với `loom-api` và Lakekeeper — và nó sẽ chia phần
  để làm MỘT việc mà một request HTTP tới `/internal/schedule/tick` đã làm hộ.

* **Không Kubernetes.** Chủ dự án chấp nhận món nợ "loom-api ghi được lên
  Kubernetes" với ĐÚNG điều kiện phạm vi hẹp nhất có thể — đúng MỘT module,
  `loom_api.jobs` — và phải CANH ĐƯỢC bằng máy
  (`services/api/tests/test_k8s_client_guard.py`). Một scheduler tự phóng Job
  biến tính chất đó từ "một module" thành "một module MỖI service", tức là làm
  yếu chính cái guard mà sự chấp nhận kia dựa vào. Việc phóng Job nạp vẫn là
  của `loom-api`, và tick chỉ là một lời NHỜ qua HTTP.

Đọc **AST** bằng `ast.parse`, KHÔNG import module. Import kéo theo cả cây phụ
thuộc gián tiếp — `httpx` một mình kéo theo hàng chục module — nên soi
`sys.modules` sau khi import cho báo động giả, rồi người ta nới allowlist cho
tới khi nó không canh gì nữa. Chỉ cây cú pháp mới nói được module NÀO trong mã
nguồn thật sự viết ra dòng import đó.

Phép canh có HAI lớp, và lớp thứ hai không thừa:

1. `BANNED_ROOTS` — danh sách CẤM, đúng hai tính chất ở trên, kèm lý do đọc
   được trong thông báo hỏng.
2. `ALLOWED` — danh sách CHO PHÉP. Một danh sách cấm chỉ chặn được những cái
   tên đã nghĩ ra: `psycopg2` (gốc `psycopg2`, KHÁC `psycopg`), `sqlmodel`,
   `databases`, `asyncpg`-wrapper nào đó — mỗi cái đều mở lại đúng cái lỗ mà
   `BANNED_ROOTS` tồn tại để bịt, mà không khớp một dòng nào trong nó.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "loom_scheduler"

# Gốc BỊ CẤM, kèm lý do — thông báo hỏng phải nói được VÌ SAO, không chỉ
# "ngoài danh sách".
WHY_BANNED = {
    "sqlalchemy": "database — scheduler tốn 0 connection slot Aiven, xem docstring đầu file",
    "asyncpg": "database — driver Postgres, cùng lý do sqlalchemy",
    "psycopg": "database — driver Postgres, cùng lý do sqlalchemy",
    "psycopg2": "database — driver Postgres, cùng lý do sqlalchemy",
    "alembic": "migration — schema là việc của loom-api, không phải của cái đồng hồ",
    "kubernetes": (
        "k8s — ĐÚNG MỘT module trong cả repo được chạm Kubernetes API "
        "(`loom_api.jobs`, xem services/api/tests/test_k8s_client_guard.py); "
        "phóng Job là việc tick NHỜ loom-api làm, không phải việc scheduler tự làm"
    ),
}
BANNED_ROOTS = frozenset(WHY_BANNED)

# Danh sách CHO PHÉP — lớp thứ hai, xem docstring đầu file. Mỗi mục ở đây là
# một thứ cái đồng hồ thật sự cần: đếm giờ (`asyncio`), gửi một request HTTP
# (`httpx`), đọc cấu hình (`pydantic*`), ghi log (`structlog`/`logging`), và
# tắt êm khi nhận tín hiệu (`signal`).
#
# `loom_core` KHÔNG có ở đây, dù service này import `loom_core.internal_auth`
# — nó được cho qua ở cấp MODULE ĐẦY ĐỦ bên dưới, cùng khuôn
# `test_connector_no_io.py`: `loom_core` nói chung KHÔNG vô hại (`loom_core.db`
# dựng engine SQLAlchemy, `loom_core.config` mang cả cấu hình database), nên
# cho cả package qua sẽ mở lại lỗ "không database" bằng một cửa sau.
ALLOWED = {
    "loom_scheduler",
    "asyncio",
    "collections",
    "contextlib",
    "dataclasses",
    "functools",
    "logging",
    "signal",
    "types",
    "typing",
    "httpx",
    "pydantic",
    "pydantic_settings",
    "structlog",
    "__future__",
}

# Cấp MODULE ĐẦY ĐỦ, không cấp package — xem chú thích trên `ALLOWED`.
# `loom_core.internal_auth` là một module thuần hằng số chuỗi (tên ba header bí
# mật chia sẻ); dùng chung nó là cách DUY NHẤT để tên header không lệch giữa
# bên gửi (đây) và bên kiểm (`loom_api.internal_security`).
ALLOWED_MODULES = {"loom_core.internal_auth"}


def _modules() -> list[Path]:
    found = sorted(SRC.rglob("*.py"))
    assert found, f"không thấy module nào trong {SRC} — phép kiểm này sẽ xanh oan"
    return found


def _imports(path: Path) -> list[tuple[str, int]]:
    """(tên ĐẦY ĐỦ của module được import, số dòng) cho mọi câu import trong file.

    Tên đầy đủ chứ không chỉ phần gốc: `ALLOWED_MODULES` cần phân biệt
    `loom_core.internal_auth` với `loom_core.db`, và cắt về gốc ngay tại đây thì
    hai cái đó không còn phân biệt được. Phần gốc vẫn tính được từ giá trị trả
    về (`_root`), nên không mất thông tin nào.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        # `from . import x` có module=None và level>0 — import nội bộ, luôn hợp lệ.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module, node.lineno))
    return out


def _root(module_name: str) -> str:
    return module_name.split(".")[0]


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_touches_a_database_or_kubernetes(module: Path) -> None:
    offenders = [(name, line) for name, line in _imports(module) if _root(name) in BANNED_ROOTS]
    assert not offenders, "\n".join(
        f"{module.name}:{line} import `{name}` — {WHY_BANNED[_root(name)]}"
        for name, line in offenders
    )


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_imports_anything_outside_the_allowlist(module: Path) -> None:
    """Lớp thứ hai — xem docstring đầu file.

    `BANNED_ROOTS` chỉ chặn những cái tên đã nghĩ ra; `psycopg2` hay `sqlmodel`
    mở lại đúng cái lỗ đó mà không khớp dòng nào trong nó. Thêm một phụ thuộc
    THẬT vào service này thì sửa `ALLOWED` — nhưng phải sửa CÓ CHỦ Ý, và đó
    chính là điều phép canh này mua.
    """
    offenders = [
        (name, line)
        for name, line in _imports(module)
        if _root(name) not in ALLOWED and name not in ALLOWED_MODULES
    ]
    assert not offenders, "\n".join(
        f"{module.name}:{line} import `{name}` — ngoài ALLOWED"
        + (f" ({WHY_BANNED[_root(name)]})" if _root(name) in WHY_BANNED else "")
        for name, line in offenders
    )


def test_only_the_named_loom_core_module_gets_through(tmp_path: Path) -> None:
    """`ALLOWED_MODULES` phải hẹp tới từng MODULE, không nới thành cả package.

    Không có phép kiểm này, đổi `ALLOWED_MODULES = {"loom_core.internal_auth"}`
    thành `ALLOWED |= {"loom_core"}` là một dòng trông vô hại mà không gì báo
    đỏ — trong khi nó cho phép `from loom_core.db import build_engine`, tức là
    một pool SQLAlchemy đi vào service này qua cửa sau, đúng thứ tính chất
    "không database" tồn tại để chặn.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from loom_core.internal_auth import SCHEDULE_SHARED_SECRET_HEADER\n"
        "from loom_core.db import build_engine\n",
        encoding="utf-8",
    )
    offenders = [
        name
        for name, _line in _imports(probe)
        if _root(name) not in ALLOWED and name not in ALLOWED_MODULES
    ]
    assert offenders == ["loom_core.db"]


def test_the_guard_can_see_a_violation(tmp_path: Path) -> None:
    """Phép canh cho chính phép canh.

    `_imports` là thứ DUY NHẤT đứng giữa một `import sqlalchemy` và việc nó lọt
    qua. Nếu nó bỏ sót một DẠNG câu import — `from x import y`, `import x.y`,
    import trong hàm, import lồng trong `if` — thì hai phép trên xanh mà không
    canh gì cả.

    Kiểm bằng mã giả lập chứa đủ các dạng, thay vì tin rằng `ast.walk` bắt hết.
    """
    source = (
        "import sqlalchemy\n"
        "import kubernetes.client\n"
        "from alembic import command\n"
        "def f():\n"
        "    import asyncpg\n"
        "if True:\n"
        "    from psycopg import connect\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")

    found = _imports(probe)
    assert found == [
        ("sqlalchemy", 1),
        ("kubernetes.client", 2),
        ("alembic", 3),
        ("asyncpg", 5),
        ("psycopg", 7),
    ]
    # Và cả năm đều phải bị `BANNED_ROOTS` bắt — `_root` là chỗ
    # `kubernetes.client` trở lại thành `kubernetes`.
    assert all(_root(name) in BANNED_ROOTS for name, _ in found)
