"""Phép canh hàng rào Aiven của các script ĐO trong `scripts/`.

Hai sự cố THẬT sinh ra hàng rào này, cả hai trên service Aiven của chủ dự án
trong lúc control plane của Loom đang sống trên đó:

1. Một phép đo nạp bảng bench làm hết đĩa và Aiven lật CẢ service sang chỉ-đọc;
   ngay cả `DROP SCHEMA` dọn dẹp cũng bị từ chối.
2. Giết một pod đang đọc giữa chừng làm hết connection slot. Service có
   `max_connections=20` và nó dùng CHUNG với một ứng dụng KHÁC của chủ dự án.

Kỷ luật rút ra được viết vào `scripts/_aiven_guard.py`. File này canh rằng nó
KHÔNG TRÔI — vì bản trước đã trôi thật: hai script giữ hai bản chép của cùng một
hàng rào, mỗi bản nhớ một nửa bài học, và không bản nào đủ.

**Đây là phép canh TĨNH, và giới hạn của nó phải nói ra.** Nó đọc mã bằng `ast`,
không mở một connection nào. Nó bắt được "một script dựng DSN Aiven bằng tay" —
đúng cái đã xảy ra. Nó KHÔNG bắt được một script gọi `subprocess` chạy `psql`,
hay đọc Aiven qua SQLAlchemy. Một phép canh tĩnh không thể hứa nhiều hơn thứ nó
đọc được, nên nó chỉ hứa đúng phần đó.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
GUARD_PATH = SCRIPTS_DIR / "_aiven_guard.py"

# Hàm SINH RA một DSN Aiven. Truyền kết quả của chúng vào `psycopg.connect`
# thẳng là bỏ qua `connect_read_only` — chỗ ĐỌC LẠI `SHOW
# default_transaction_read_only` từ chính server. Một `options` sai chính tả vẫn
# cho connect thành công và im lặng bỏ qua tham số, nên chuỗi DSN một mình không
# chứng minh được gì.
DSN_PRODUCERS = frozenset({"aiven_dsn", "dsn_from_environ", "build_read_only_dsn"})

# `grant_tenant_admin.py` chạm Aiven nhưng KHÔNG phải script đo: nó là đường
# bootstrap cấp quyền cho admin ĐẦU TIÊN và nó PHẢI ghi (một `INSERT` vào
# `role_assignment`). Nó dùng SQLAlchemy, không dùng psycopg, nên nó rơi ra
# ngoài bộ lọc bên dưới một cách tự nhiên — nhưng nói thẳng ra đây, vì một
# ngoại lệ ĐƯỢC ĐẶT TÊN thì có người xem lại, còn một lỗ hổng thì không.
NON_MEASUREMENT_AIVEN_SCRIPTS = frozenset({"grant_tenant_admin.py"})


def _script_paths() -> list[Path]:
    return sorted(p for p in SCRIPTS_DIR.glob("*.py") if p.name != GUARD_PATH.name)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _imports_psycopg(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "psycopg" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "psycopg":
            return True
    return False


def _imports_guard(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "_aiven_guard" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "_aiven_guard":
            return True
    return False


def _called_name(node: ast.AST) -> str | None:
    """Tên hàm của một lời gọi, cho cả `f()` lẫn `mod.f()`."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _dsn_producing_names(tree: ast.Module) -> set[str]:
    """Tên biến VÀ tên hàm mang một DSN Aiven trong module này.

    Hai vòng, vì một script bọc hàm sinh DSN trong hàm riêng của nó
    (`measure_ingest_path._dsn_from_env`) và phép canh phải đi qua được một lớp
    bọc — nếu không thì cách lách dễ nhất lại là cách trông vô hại nhất.
    """
    producers = set(DSN_PRODUCERS)
    for _ in range(2):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if _called_name(node.value) in producers:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            producers.add(target.id)
            elif isinstance(node, ast.FunctionDef):
                returns = [
                    child
                    for child in ast.walk(node)
                    if isinstance(child, ast.Return) and child.value is not None
                ]
                if returns and all(_called_name(r.value) in producers for r in returns):
                    producers.add(node.name)
    return producers


def _psycopg_connect_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "psycopg"
        ):
            calls.append(node)
    return calls


# ───────────────────────── hàng rào chỉ-đọc ─────────────────────────


def test_guard_declares_read_only_and_bakes_it_into_the_dsn() -> None:
    """Tham số chỉ-đọc phải nằm TRONG chuỗi DSN, không nằm trong một docstring."""
    source = GUARD_PATH.read_text()
    assert "default_transaction_read_only=on" in source, (
        "scripts/_aiven_guard.py không còn khai `default_transaction_read_only=on`. "
        "Đó là CẢ hàng rào chỉ-đọc — không có nó, mọi script đo mở connection GHI ĐƯỢC "
        "vào service đang chở control plane."
    )

    tree = _parse(GUARD_PATH)
    option_value: str | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "READ_ONLY_OPTION"
            and isinstance(node.value, ast.Constant)
        ):
            option_value = str(node.value.value)
    assert option_value is not None, "không tìm thấy hằng READ_ONLY_OPTION trong _aiven_guard.py"
    assert "default_transaction_read_only=on" in option_value, (
        f"READ_ONLY_OPTION = {option_value!r} không bật chế độ chỉ-đọc."
    )


def _non_docstring_constants(tree: ast.Module) -> list[str]:
    """Mọi chuỗi trong MÃ, trừ docstring.

    Phân biệt này là cả điểm: kể lại sự cố trong docstring là thứ PHẢI giữ, còn
    dựng lại tham số DSN trong mã là thứ phải chặn. Chú thích `#` không nằm
    trong AST nên chúng tự rơi ra ngoài.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_only_the_guard_builds_the_read_only_option() -> None:
    """Một bản chép thứ hai là một chỗ để quên `on` mà không ai thấy."""
    offenders: list[str] = []
    for path in _script_paths():
        for text in _non_docstring_constants(_parse(path)):
            if "default_transaction_read_only=on" in text:
                offenders.append(path.name)
    assert offenders == [], (
        f"{sorted(set(offenders))} tự dựng tham số DSN `default_transaction_read_only=on`. "
        "Hàng rào phải có ĐÚNG MỘT định nghĩa, trong `scripts/_aiven_guard.py` — bản chép "
        "trước đã trôi và mỗi bản chỉ nhớ một nửa bài học của một sự cố khác nhau."
    )


def test_no_script_hand_rolls_an_aiven_dsn() -> None:
    """Một chuỗi vừa có `host=` vừa có `password=` là một DSN dựng tay."""
    offenders: list[str] = []
    for path in _script_paths():
        for text in _non_docstring_constants(_parse(path)):
            if "password=" in text and ("host=" in text or "sslrootcert=" in text):
                offenders.append(f"{path.name}: {text[:60]!r}")
    assert offenders == [], (
        "DSN dựng tay ngoài hàng rào:\n  " + "\n  ".join(offenders) + "\n"
        "Dùng `_aiven_guard.build_read_only_dsn` — nó là đường DUY NHẤT, và không có "
        "tham số nào tắt được chế độ chỉ-đọc."
    )


@pytest.mark.parametrize("path", _script_paths(), ids=lambda p: p.name)
def test_aiven_dsn_never_reaches_raw_psycopg_connect(path: Path) -> None:
    """DSN Aiven chỉ được mở qua `connect_read_only`, không qua `psycopg.connect`.

    `psycopg.connect` vẫn HỢP LỆ cho Postgres cục bộ do testcontainers dựng —
    container đó dùng xong thì vứt, và không có control plane nào sống trên nó.
    Phép canh vì thế bám vào NGUỒN của chuỗi DSN chứ không cấm lời gọi.
    """
    tree = _parse(path)
    producers = _dsn_producing_names(tree)
    for call in _psycopg_connect_calls(tree):
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            name = arg.id if isinstance(arg, ast.Name) else _called_name(arg)
            assert name not in producers, (
                f"{path.name}:{call.lineno} mở một DSN Aiven ({name}) bằng `psycopg.connect`. "
                "Dùng `_aiven_guard.connect_read_only` — nó ĐỌC LẠI `SHOW "
                "default_transaction_read_only` từ server, thứ mà chuỗi DSN một mình "
                "không chứng minh được."
            )


@pytest.mark.parametrize("path", _script_paths(), ids=lambda p: p.name)
def test_every_psycopg_script_goes_through_the_guard(path: Path) -> None:
    """Script đo nào mở Postgres cũng phải nạp hàng rào."""
    if path.name in NON_MEASUREMENT_AIVEN_SCRIPTS:
        pytest.skip(f"{path.name}: đường bootstrap admin, PHẢI ghi — ngoại lệ có tên")
    tree = _parse(path)
    if not _imports_psycopg(tree):
        return
    assert _imports_guard(tree), (
        f"{path.name} import psycopg nhưng không import `_aiven_guard`. Mọi script đo "
        "chạm Postgres phải lấy DSN Aiven từ hàng rào chung."
    )


def test_no_script_writes_rows_into_the_source() -> None:
    """`COPY ... FROM STDIN` là đúng câu đã lấp đầy đĩa và lật service sang chỉ-đọc.

    Chỉ soi chuỗi trong MÃ: `COPY ... TO STDOUT` (một phép ĐỌC, và là cách
    `probe_read_path_cost` đo trần đường truyền) phải vẫn hợp lệ, còn khối chú
    thích kể lại sự cố thì phải giữ được.
    """
    offenders: list[str] = []
    for path in _script_paths():
        for text in _non_docstring_constants(_parse(path)):
            upper = " ".join(text.upper().split())
            if "FROM STDIN" in upper:
                offenders.append(f"{path.name}: {text.strip()[:60]!r}")
    assert offenders == [], (
        "script đo đang GHI dữ liệu vào nguồn:\n  " + "\n  ".join(offenders) + "\n"
        "Dùng `generate_series` — nó sinh dòng phía server, không chạm một page nào "
        "trên đĩa và không sinh một byte WAL nào."
    )


# ─────────────────── kiểm dung lượng sau MỖI khối ───────────────────


class _FakeCursor:
    """Cursor giả trả một chuỗi số đo đĩa đã định sẵn."""

    def __init__(self, readings: list[int]) -> None:
        self._readings = list(readings)
        self._last: int | None = None

    def execute(self, query: str, params: object = None) -> None:
        self._last = self._readings.pop(0) if self._readings else 0

    def fetchone(self) -> tuple[int]:
        return (self._last if self._last is not None else 0,)


def _guard_module():  # type: ignore[no-untyped-def]
    import importlib.util
    import sys

    name = "_aiven_guard_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ĐĂNG KÝ TRƯỚC khi exec: `from __future__ import annotations` làm mọi
    # annotation thành chuỗi, và `dataclasses` giải chúng bằng
    # `sys.modules[cls.__module__].__dict__`. Thiếu dòng này thì nó tra vào
    # `None` và vỡ ở chỗ chẳng liên quan gì tới hàng rào.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_headroom_records_every_block_not_just_the_first() -> None:
    """Bản đầu kiểm ĐÚNG MỘT LẦN và service lật sang chỉ-đọc ở dòng thứ 1.000.000."""
    guard = _guard_module()
    headroom = guard.StorageHeadroom(ceiling_bytes=500_000_000)
    cur = _FakeCursor([100_000_000, 120_000_000, 140_000_000])

    for i in range(3):
        headroom.check(cur, f"khối {i}")

    assert len(headroom.checks) == 3, "phải giữ lại MỘT phép đo cho MỖI khối"
    assert headroom.first_seen == 100_000_000
    assert headroom.last_seen == 140_000_000
    assert headroom.delta == 40_000_000


def test_headroom_refuses_when_a_later_block_crosses_the_ceiling() -> None:
    """Khối đầu an toàn KHÔNG chứng minh khối sau an toàn — WAL cộng vào cùng volume."""
    guard = _guard_module()
    headroom = guard.StorageHeadroom(ceiling_bytes=150_000_000)
    cur = _FakeCursor([100_000_000, 200_000_000])

    headroom.check(cur, "khối 0")  # dưới trần, đi tiếp
    with pytest.raises(SystemExit) as excinfo:
        headroom.check(cur, "khối 1")
    assert "vượt trần" in str(excinfo.value)


def test_headroom_counts_planned_bytes_before_they_are_written() -> None:
    """Trần phải chặn TRƯỚC khi ghi, không phát hiện SAU khi đã qua mép."""
    guard = _guard_module()
    headroom = guard.StorageHeadroom(ceiling_bytes=150_000_000)
    cur = _FakeCursor([100_000_000])
    with pytest.raises(SystemExit):
        headroom.check(cur, "trước khi nạp", planned_bytes=90_000_000)
