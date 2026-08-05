import re

from sqlalchemy import CheckConstraint

from loom_api.models import AuditLog, RoleAssignment


def _checks() -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in RoleAssignment.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name
    }


def _literals(sql: str) -> set[str]:
    """Các hằng chuỗi trong một mệnh đề CHECK."""
    return set(re.findall(r"'([^']*)'", sql))


def test_principal_is_exactly_one_of_user_or_group() -> None:
    """Cho phép cả hai NULL thì có hàng không thuộc về ai; cho phép cả hai có giá
    trị thì không rõ hàng nói về ai. CHECK ép đúng một."""
    sql = " ".join(_checks().values())
    assert "num_nonnulls" in sql
    one = _checks()["ck_role_assignment_one_principal"]
    # So khớp CHÍNH XÁC, không phải `"= 1" in ...`: `>= 1` chứa chuỗi con "= 1"
    # nhưng cho qua đúng cái hàng mơ hồ mà constraint này sinh ra để chặn.
    assert re.sub(r"\s+", "", one) == "num_nonnulls(principal_user_id,principal_group)=1"


def test_role_and_scope_type_are_constrained() -> None:
    """Không có CHECK thì một lỗi gõ ('admn') lặng lẽ thành một vai trò không
    khớp gì cả — người dùng mất quyền và không có thông báo lỗi nào.

    Kiểm TỪNG constraint theo tên, không phải nối hết lại rồi tìm chuỗi con: nối
    lại thì một CHECK gán nhầm danh sách vai trò cho cột scope_type vẫn xanh."""
    checks = _checks()
    role_sql = checks["ck_role_assignment_role"]
    for role in ("viewer", "contributor", "member", "admin"):
        assert role in role_sql
    assert role_sql.split()[0] == "role"

    scope_sql = checks["ck_role_assignment_scope_type"]
    for scope in ("tenant", "domain", "workspace", "item"):
        assert scope in scope_sql
    assert scope_sql.split()[0] == "scope_type"

    principal_sql = checks["ck_role_assignment_principal_type"]
    for principal in ("user", "group"):
        assert principal in principal_sql


def test_check_role_values_match_the_single_source_of_truth() -> None:
    """`roles.py` là nguồn duy nhất. Nếu CHECK và enum lệch nhau thì một vai trò
    hợp lệ trong Python bị database từ chối, hoặc ngược lại — và không có gì
    trong hai file đó nhắc bạn."""
    from loom_core.roles import Role

    # Bằng NHAU cả hai chiều: thiếu một vai trò thì database từ chối thứ Python
    # cho phép; thừa một vai trò thì database nhận thứ Python không hiểu.
    assert _literals(_checks()["ck_role_assignment_role"]) == {r.name for r in Role}


def test_check_scope_types_match_the_scope_chain() -> None:
    """SCOPE_CHAIN quyết định vòng lặp thừa kế quyền. Một scope_type mà database
    nhận nhưng SCOPE_CHAIN không có sẽ tạo assignment không bao giờ có tác dụng."""
    from loom_core.roles import SCOPE_CHAIN

    assert _literals(_checks()["ck_role_assignment_scope_type"]) == set(SCOPE_CHAIN)


def test_scope_id_has_no_foreign_key() -> None:
    """CỐ Ý không có FK: scope_id trỏ tới bốn bảng khác nhau tuỳ scope_type.
    Đánh đổi được ghi trong spec mục 3.1 — giảm thiểu bằng xoá mềm."""
    assert RoleAssignment.__table__.c.scope_id.foreign_keys == set()


def test_assignment_is_unique_per_principal_and_scope() -> None:
    """Unique THƯỜNG không đủ. Postgres coi hai NULL là khác nhau, nên hai hàng
    y hệt cho cùng một nhóm (cả hai principal_user_id = NULL) lọt qua. Chỉ
    NULLS NOT DISTINCT chặn được — xem proof chạy trên database thật."""
    names = {c.name for c in RoleAssignment.__table__.constraints if c.name}
    names |= {i.name for i in RoleAssignment.__table__.indexes}
    assert "uq_role_assignment_principal_scope" in names

    idx = {i.name: i for i in RoleAssignment.__table__.indexes}
    target = idx["uq_role_assignment_principal_scope"]
    assert target.unique is True
    # Chính là thứ mà test tên-suông ở trên không thấy.
    assert target.dialect_options["postgresql"]["nulls_not_distinct"] is True
    assert [c.name for c in target.columns] == [
        "principal_user_id",
        "principal_group",
        "scope_type",
        "scope_id",
    ]


def test_audit_carries_request_id_for_log_correlation() -> None:
    """request_id nối audit với log và trace của Giai đoạn 0. Không có nó thì từ
    một dòng audit không có đường nào tới log của đúng request."""
    assert AuditLog.__table__.c.request_id.nullable is False


def test_audit_workspace_index_is_partial() -> None:
    """workspace_id NULL với thao tác cấp tenant/domain. Index một phần bỏ hẳn
    những hàng đó thay vì mang chúng theo mãi."""
    idx = {i.name: i for i in AuditLog.__table__.indexes}
    target = idx["ix_audit_workspace"]
    where = target.dialect_options["postgresql"]["where"]
    assert where is not None
    assert "workspace_id" in str(where)
