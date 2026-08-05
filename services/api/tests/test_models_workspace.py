from sqlalchemy import UniqueConstraint

from loom_api.models import Domain, Workspace


def test_workspace_has_soft_delete_and_partial_unique_index() -> None:
    """Unique THƯỜNG cộng xoá mềm là một cái bẫy: xoá workspace tên `retail` rồi
    tạo lại cùng tên sẽ bị chặn, kèm thông báo lỗi vô nghĩa vì hàng cũ người dùng
    không còn thấy. Index phải có WHERE state = 'active'."""
    idx = {i.name: i for i in Workspace.__table__.indexes}
    assert "uq_workspace_active_name" in idx
    target = idx["uq_workspace_active_name"]
    assert target.unique is True
    where = target.dialect_options["postgresql"]["where"]
    assert where is not None
    # `is not None` một mình vẫn xanh với WHERE true. Điều kiện phải nói về
    # state, nếu không cái bẫy xoá-mềm vẫn còn nguyên.
    assert "state" in str(where)
    assert "active" in str(where)
    assert [c.name for c in target.columns] == ["tenant_id", "name"]


def test_workspace_domain_is_optional_and_set_null_on_delete() -> None:
    """Xoá một domain KHÔNG được kéo theo workspace bên trong — đó là mất dữ liệu
    do một thao tác phân loại. SET NULL để workspace thành 'chưa phân domain'."""
    fk = next(iter(Workspace.__table__.c.domain_id.foreign_keys))
    assert fk.ondelete == "SET NULL"
    assert Workspace.__table__.c.domain_id.nullable is True


def test_domain_name_unique_per_tenant() -> None:
    """Tên domain duy nhất trong một tenant. Domain KHÔNG xoá mềm nên ở đây
    unique thường là đúng — khác workspace và item."""
    uniques = [c for c in Domain.__table__.constraints if isinstance(c, UniqueConstraint)]
    assert [c.name for c in uniques] == ["uq_domain_tenant_name"]
    assert [x.name for x in uniques[0].columns] == ["tenant_id", "name"]


def test_storage_prefix_is_not_nullable() -> None:
    """Giai đoạn 2 dùng cột này để chia prefix trên object storage. Cho phép NULL
    thì Giai đoạn 2 phải backfill trong khi có dữ liệu thật — đắt và dễ sai."""
    assert Workspace.__table__.c.storage_prefix.nullable is False
