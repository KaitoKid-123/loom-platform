from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from loom_api.models import Item, ItemVersion


def test_item_active_name_unique_is_partial() -> None:
    idx = {i.name: i for i in Item.__table__.indexes}
    target = idx["uq_item_active_name"]
    assert target.unique is True
    where = target.dialect_options["postgresql"]["where"]
    assert where is not None
    assert "state" in str(where)
    assert "active" in str(where)
    assert [c.name for c in target.columns] == ["workspace_id", "type", "name"]


def test_pagination_index_matches_sort_order_exactly() -> None:
    """Cursor sắp theo (updated_at DESC, id DESC) — xem `list_items` của Giai
    đoạn 1b. Index phải khớp ĐÚNG thứ tự đó, nếu không Postgres bỏ index và mọi
    lần lật trang là một sequential scan — chạy tốt với 20 item, sụp với 20 nghìn.

    Cả BA cột đều ASC là CỐ Ý, không phải bỏ sót. Btree quét ngược một index
    (workspace_id, updated_at, id) cho ra đúng (updated_at DESC, id DESC) khi
    workspace_id là điều kiện bằng. Nếu đặt `updated_at DESC, id ASC` như DDL
    trong spec mục 3.1 thì KHÔNG có chiều quét nào cho ra thứ tự cursor cần, và
    planner phải Sort — đúng cái mà index này sinh ra để tránh."""
    idx = {i.name: i for i in Item.__table__.indexes}
    target = idx["ix_item_pagination"]
    assert [c.name for c in target.columns] == ["workspace_id", "updated_at", "id"]
    # `columns` không nói gì về chiều sắp xếp: một index khai báo
    # `updated_at DESC` cho ra danh sách cột y hệt. Chốt bằng DDL đã biên dịch,
    # thứ duy nhất chứa cả thứ tự, chiều và mệnh đề WHERE.
    ddl = str(CreateIndex(target).compile(dialect=postgresql.dialect())).strip()
    assert ddl == (
        "CREATE INDEX ix_item_pagination ON item (workspace_id, updated_at, id) "
        "WHERE state = 'active'"
    )


def test_item_version_stores_metadata_not_just_definition() -> None:
    """restore phải phục hồi được cả display_name và folder_path. Nếu version chỉ
    giữ definition thì hoàn tác một lần đổi tên là bất khả, và người dùng phát
    hiện điều đó đúng lúc họ cần nó nhất."""
    cols = set(ItemVersion.__table__.c.keys())
    assert {"definition", "display_name", "folder_path", "description"} <= cols
    # Ba cột metadata phải NOT NULL trừ description — một bản version thiếu
    # display_name thì restore không có gì để khôi phục về.
    assert ItemVersion.__table__.c.display_name.nullable is False
    assert ItemVersion.__table__.c.folder_path.nullable is False


def test_item_version_cascades_with_item() -> None:
    fk = next(iter(ItemVersion.__table__.c.item_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


def test_definition_hash_and_version_are_both_present_and_distinct() -> None:
    """Hai con số, hai việc: version là ETag (phủ mọi trường), definition_hash là
    dấu vết nội dung cho Git drift ở Giai đoạn 5. Gộp lại thì đổi tên không bị
    phát hiện xung đột — xem spec mục 2.2."""
    cols = Item.__table__.c
    assert cols.version.nullable is False
    assert cols.definition_hash.nullable is False
    # Hai cột RIÊNG BIỆT, không phải một cột dùng hai tên: gộp lại là chính cái
    # bug docstring đang cảnh báo.
    assert cols.version is not cols.definition_hash
    assert cols.version.name != cols.definition_hash.name


def test_item_version_pair_is_unique() -> None:
    """Hai hàng cùng (item_id, version) làm `restore version 3` thành một câu hỏi
    không có câu trả lời."""
    names = {c.name for c in ItemVersion.__table__.constraints if c.name}
    assert "uq_item_version" in names
