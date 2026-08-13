"""Migration là hợp đồng, không phải một lần chạy.

Ba lớp trong file này, cố ý tách rời:

1. **Schema có mặt** — bản cũ chỉ có phần này, và nó KHÔNG bắt được drift.
2. **`alembic check`** — model và migration không lệch nhau. Giai đoạn 0 từng có
   index nằm trong migration mà thiếu trong model, nên `autogenerate` lần sau sẽ
   DROP chúng; test "bảng có tồn tại" mù hoàn toàn với việc đó.
3. **Hành vi của DDL trên Postgres thật** — bốn phép chứng minh chuyển vào đây
   từ script dùng một lần của Task 5-7. Khác biệt quan trọng: script cũ dựng
   bảng nháp từ DDL của MODEL, nên nó chứng minh "DDL này hành xử như vậy".
   Ở đây chúng chạy trên **schema đã migrate**, tức chứng minh "MIGRATION sinh
   ra đúng DDL đó" — đó mới là câu hỏi thật, vì thứ chạy trên Aiven là migration
   chứ không phải model.

Vì sao phải là hành vi chứ không chỉ đọc `indexdef`: mất `postgresql_where` biến
unique-một-phần thành unique thường (không tạo lại được tên đã xoá mềm), mất
`NULLS NOT DISTINCT` cho hai hàng cấp quyền trùng nhau cho cùng một nhóm lọt
qua. Cả hai hỏng ÂM THẦM — không có lỗi nào ở lúc migrate.
"""

import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from testcontainers.community.postgres import PostgresContainer

from loom_api.models import DEFAULT_TENANT_ID

from .pg_support import POSTGRES_IMAGE, head_revision, run_alembic, sync_url

pytestmark = pytest.mark.integration

NEW_TABLES = frozenset(
    {
        "domain",
        "workspace",
        "item",
        "item_version",
        "role_assignment",
        "audit_log",
        "ingest_run",
        "stream_state",
    }
)

# Năm index một phần và một index NULLS NOT DISTINCT — đúng những thứ mà một
# bản autogenerate cẩu thả đánh rơi mà vẫn tạo được index.
PARTIAL_INDEXES = {
    "uq_workspace_active_name": "state = 'active'",
    "uq_item_active_name": "state = 'active'",
    "ix_item_workspace_folder": "state = 'active'",
    "ix_item_pagination": "state = 'active'",
    "ix_audit_workspace": "workspace_id IS NOT NULL",
}


# `migrated_pg` giờ ở conftest.py, phạm vi SESSION: test quyền của Task 9 chạy
# trên đúng schema đã migrate này, và một container thứ hai cho cùng một schema
# chỉ tốn thêm ba giây initdb. Mọi test bên dưới vẫn soi/đụng vào SCHEMA ĐÃ
# MIGRATE — không có test nào dựng bảng từ metadata của model, vì như thế là
# kiểm nhầm thứ.


@pytest.fixture(scope="module")
def engine(migrated_pg: PostgresContainer) -> Iterator[sa.Engine]:
    eng = sa.create_engine(sync_url(migrated_pg))
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: sa.Engine) -> Iterator[sa.Connection]:
    """Một transaction bị ROLLBACK sau mỗi test.

    Nhờ vậy các test hành vi ghi thoải mái vào bảng thật mà không để lại gì, và
    không test nào phụ thuộc thứ tự chạy. Các phép ghi ĐƯỢC PHÉP HỎNG đi qua
    savepoint (`_attempt`) — một IntegrityError sẽ huỷ cả transaction nếu không.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _attempt(conn: sa.Connection, sql: str, **params: object) -> str | None:
    """Chạy một câu lệnh trong savepoint. Trả None nếu ĐƯỢC NHẬN, hoặc tên
    constraint đã từ chối nó.

    Trả về TÊN constraint chứ không phải bool: "bị từ chối" mà không biết vì cái
    gì thì một hàng bị chặn bởi NOT NULL cũng trông y hệt như bị chặn bởi đúng
    index mình đang muốn kiểm.
    """
    savepoint = conn.begin_nested()
    try:
        conn.execute(sa.text(sql), params)
    except sa.exc.IntegrityError as exc:
        savepoint.rollback()
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        return str(name) if name else type(exc.orig).__name__
    savepoint.commit()
    return None


@pytest.fixture
def actor(conn: sa.Connection) -> uuid.UUID:
    """Một app_user thật để thoả các FK created_by/updated_by."""
    user_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO app_user (id, tenant_id, subject, email, display_name)"
            " VALUES (:id, :tenant, :subject, 'proof@loom.local', 'Proof')"
        ),
        {"id": user_id, "tenant": DEFAULT_TENANT_ID, "subject": f"proof-{user_id}"},
    )
    return user_id


def _new_workspace(conn: sa.Connection, actor: uuid.UUID, name: str) -> uuid.UUID:
    ws_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO workspace (id, tenant_id, name, display_name, storage_prefix,"
            " created_by, updated_by)"
            " VALUES (:id, :tenant, :name, :name, 's3://x', :actor, :actor)"
        ),
        {"id": ws_id, "tenant": DEFAULT_TENANT_ID, "name": name, "actor": actor},
    )
    return ws_id


def _new_item(
    conn: sa.Connection, actor: uuid.UUID, ws: uuid.UUID, item_type: str, name: str
) -> uuid.UUID:
    """Một item thật — `ingest_run`/`stream_state` có FK tới `item.id`, nên
    `lakehouse_id`/`connection_id` phải trỏ vào hàng có thật, không phải chèn
    được UUID bất kỳ."""
    item_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO item (id, tenant_id, workspace_id, type, name, display_name,"
            " definition, definition_hash, created_by, updated_by)"
            " VALUES (:id, :tenant, :ws, :type, :name, :name, '{}'::jsonb, :hash, :actor, :actor)"
        ),
        {
            "id": item_id,
            "tenant": DEFAULT_TENANT_ID,
            "ws": ws,
            "type": item_type,
            "name": name,
            "hash": "x" * 64,
            "actor": actor,
        },
    )
    return item_id


# ---------------------------------------------------------------- lớp 1: schema


def test_migration_creates_schema_and_seeds_tenant(
    migrated_pg: PostgresContainer, engine: sa.Engine
) -> None:
    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert {"tenant", "app_user", "user_session", "alembic_version"} <= tables
        assert tables >= NEW_TABLES, f"thiếu bảng: {sorted(NEW_TABLES - tables)}"
        count = connection.execute(sa.text("SELECT count(*) FROM tenant")).scalar_one()
        assert count == 1
        version = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert version == head_revision()


def test_partial_and_nulls_not_distinct_survive_into_the_real_schema(
    engine: sa.Engine,
) -> None:
    """Đọc thẳng `pg_indexes.indexdef` của bảng THẬT.

    Đây là phần mà `alembic check` KHÔNG canh: check so model với database, nên
    nếu một ngày nào đó cả hai cùng đánh rơi mệnh đề WHERE thì check vẫn xanh.
    Danh sách dưới đây là hằng số viết tay, độc lập với cả model lẫn migration.
    """
    with engine.connect() as connection:
        defs = dict(
            connection.execute(
                sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
            ).all()
        )

    for name, predicate in PARTIAL_INDEXES.items():
        assert name in defs, f"{name} không tồn tại trong schema đã migrate"
        assert " WHERE " in defs[name], (
            f"{name} là index MỘT PHẦN nhưng migration tạo ra index TOÀN PHẦN: {defs[name]}"
        )
        column = predicate.split()[0]
        assert column in defs[name].split(" WHERE ", 1)[1]

    nnd = defs["uq_role_assignment_principal_scope"]
    assert "NULLS NOT DISTINCT" in nnd, (
        "mất NULLS NOT DISTINCT thì hai hàng cấp quyền y hệt cho cùng một nhóm "
        f"đều lọt qua, im lặng: {nnd}"
    )


def test_all_four_role_assignment_checks_reach_the_real_table(engine: sa.Engine) -> None:
    """`alembic check` KHÔNG so CHECK constraint — autogenerate không phát hiện
    được một CHECK bị thêm hay bớt trên database. Nên bốn cái này cần một phép
    kiểm đọc thẳng `pg_constraint` của bảng đã migrate.

    Riêng `ck_role_assignment_one_principal`: từ khi
    `ck_role_assignment_principal_type` buộc principal_type khớp với cột, vế
    num_nonnulls trở thành HỆ QUẢ logic của nó — không còn hàng nào chỉ mình nó
    chặn được. Nên không có phép kiểm HÀNH VI nào cô lập được nó nữa, và đây là
    thứ duy nhất canh việc nó biến mất. Giữ lại vì nó phát biểu bất biến một
    cách độc lập và rẻ, không phải vì nó còn chặn thêm hàng nào.
    """
    with engine.connect() as connection:
        names = {
            r[0]
            for r in connection.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = 'role_assignment'::regclass AND contype = 'c'"
                )
            )
        }

    assert names == {
        "ck_role_assignment_principal_type",
        "ck_role_assignment_scope_type",
        "ck_role_assignment_role",
        "ck_role_assignment_one_principal",
    }, f"CHECK trên role_assignment lệch: {sorted(names)}"


# ------------------------------------------------------------- lớp 2: drift gate


def test_models_and_migrations_do_not_drift(migrated_pg: PostgresContainer) -> None:
    """Giai đoạn 0 từng có index nằm trong migration mà thiếu trong model —
    autogenerate lần sau sẽ DROP chúng. Test cũ chỉ kiểm bảng có tồn tại nên
    không thấy. `alembic check` so model với database và đỏ nếu lệch.

    Đã kiểm chứng cửa này thật sự đóng được: thêm một cột vào Item làm
    `alembic check` trả về 255 với `add_column`, gỡ ra thì xanh lại.
    """
    result = run_alembic(migrated_pg, "check")
    assert result.returncode == 0, (
        f"model và migration đã lệch nhau:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_downgrade_removes_exactly_the_new_tables_and_upgrade_restores_them() -> None:
    """`downgrade()` mà chưa ai chạy thì chỉ là một hàm trông hợp lý.

    Container riêng: test này hạ rồi nâng lại schema, không dùng chung được với
    các test khác trong module.
    """
    with PostgresContainer(POSTGRES_IMAGE) as pg:
        assert run_alembic(pg, "upgrade", "head").returncode == 0
        eng = sa.create_engine(sync_url(pg))

        def snapshot() -> tuple[set[str], set[str], str]:
            with eng.connect() as c:
                tables = set(sa.inspect(c).get_table_names())
                indexes = {
                    r[0]
                    for r in c.execute(
                        sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                    )
                }
                version = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            return tables, indexes, str(version)

        before = snapshot()
        assert before[0] >= NEW_TABLES
        assert before[2] == head_revision()

        assert run_alembic(pg, "downgrade", "0002").returncode == 0
        after_down = snapshot()
        assert not (NEW_TABLES & after_down[0]), (
            f"downgrade để sót bảng: {sorted(NEW_TABLES & after_down[0])}"
        )
        # Và KHÔNG được kéo theo bảng của 0001/0002.
        assert {"tenant", "app_user", "user_session"} <= after_down[0]
        assert after_down[2] == "0002"

        assert run_alembic(pg, "upgrade", "head").returncode == 0
        # So cả tập index, không chỉ tập bảng: một downgrade/upgrade đánh rơi
        # đúng một index sẽ cho tập bảng y hệt.
        assert snapshot() == before

        eng.dispose()


# --------------------------------------------- lớp 3: hành vi trên schema đã migrate


def test_partial_unique_index_allows_reusing_a_name_after_soft_delete(
    conn: sa.Connection, actor: uuid.UUID
) -> None:
    """Chứng minh 1/4 — `uq_workspace_active_name`.

    Unique THƯỜNG sẽ cho qua vế đầu và chặn vế cuối: người dùng xoá một
    workspace rồi không bao giờ tạo lại được nó với cùng cái tên, và thông báo
    lỗi nói về một hàng họ không còn nhìn thấy.
    """
    _new_workspace(conn, actor, "retail")

    duplicate = _attempt(
        conn,
        "INSERT INTO workspace (id, tenant_id, name, display_name, storage_prefix,"
        " created_by, updated_by)"
        " VALUES (gen_random_uuid(), :tenant, 'retail', 'retail', 's3://x', :actor, :actor)",
        tenant=DEFAULT_TENANT_ID,
        actor=actor,
    )
    assert duplicate == "uq_workspace_active_name", (
        f"hai workspace ACTIVE cùng tên phải bị chặn, thấy: {duplicate}"
    )

    conn.execute(sa.text("UPDATE workspace SET state = 'deleted' WHERE name = 'retail'"))

    reuse = _attempt(
        conn,
        "INSERT INTO workspace (id, tenant_id, name, display_name, storage_prefix,"
        " created_by, updated_by)"
        " VALUES (gen_random_uuid(), :tenant, 'retail', 'retail', 's3://x', :actor, :actor)",
        tenant=DEFAULT_TENANT_ID,
        actor=actor,
    )
    assert reuse is None, (
        "sau khi xoá mềm phải dùng lại được tên — mất `postgresql_where` thì "
        f"chính vế này đỏ: {reuse}"
    )


def test_pagination_index_serves_the_cursor_order_without_a_sort(
    conn: sa.Connection, actor: uuid.UUID
) -> None:
    """Chứng minh 2/4 — `ix_item_pagination`, hỏi thẳng planner.

    Cursor của Giai đoạn 1b sắp theo (updated_at DESC, id DESC). Cả ba cột của
    index đều ASC là CỐ Ý: btree quét NGƯỢC cho ra đúng thứ tự đó khi
    workspace_id là điều kiện bằng. Nếu ai đó "sửa" thành `updated_at DESC`
    theo DDL trong spec mục 3.1 thì không chiều quét nào khớp và planner phải
    Sort — vẫn ĐÚNG kết quả, chỉ là sắp xếp cả bảng cho mỗi trang.
    """
    ws = _new_workspace(conn, actor, "pagination")
    rows = 20_000
    conn.execute(
        sa.text(
            "INSERT INTO item (id, tenant_id, workspace_id, type, name, display_name,"
            " definition, definition_hash, created_by, updated_by, updated_at)"
            " SELECT gen_random_uuid(), :tenant, :ws, 'notebook', 'item-' || g, 'Item ' || g,"
            " '{}'::jsonb, md5(g::text), :actor, :actor,"
            " now() - (g || ' seconds')::interval"
            " FROM generate_series(1, :rows) g"
        ),
        {"tenant": DEFAULT_TENANT_ID, "ws": ws, "actor": actor, "rows": rows},
    )
    conn.execute(sa.text("ANALYZE item"))

    plan = "\n".join(
        r[0]
        for r in conn.execute(
            sa.text(
                "EXPLAIN SELECT id, updated_at FROM item"
                " WHERE workspace_id = :ws AND state = 'active'"
                " ORDER BY updated_at DESC, id DESC LIMIT 51"
            ),
            {"ws": ws},
        )
    )

    assert "ix_item_pagination" in plan, f"planner không dùng index phân trang:\n{plan}"
    assert "Sort" not in plan, (
        f"index phải phục vụ trực tiếp ORDER BY, nhưng planner vẫn phải Sort:\n{plan}"
    )
    assert "Seq Scan" not in plan, f"planner quét cả bảng:\n{plan}"


def test_nulls_not_distinct_rejects_a_duplicate_group_grant(
    conn: sa.Connection, actor: uuid.UUID
) -> None:
    """Chứng minh 3/4 — `uq_role_assignment_principal_scope`.

    Mặc định Postgres coi hai NULL là KHÁC nhau, nên unique thường không thấy
    hai hàng cấp quyền y hệt cho cùng một nhóm là trùng (cả hai có
    principal_user_id = NULL). Hậu quả không phải là quyền sai, mà là không thể
    thu hồi bằng một lần DELETE — gỡ một hàng thì hàng kia vẫn cấp quyền.
    """
    ws = _new_workspace(conn, actor, "grants")
    grant = (
        "INSERT INTO role_assignment (id, tenant_id, principal_type, principal_group,"
        " scope_type, scope_id, role, created_by)"
        " VALUES (gen_random_uuid(), :tenant, 'group', 'data-eng', 'workspace', :scope,"
        " 'viewer', :actor)"
    )
    args = {"tenant": DEFAULT_TENANT_ID, "actor": actor}

    assert _attempt(conn, grant, scope=ws, **args) is None

    duplicate = _attempt(conn, grant, scope=ws, **args)
    assert duplicate == "uq_role_assignment_principal_scope", (
        f"hàng cấp quyền TRÙNG cho cùng một nhóm phải bị chặn, thấy: {duplicate}"
    )

    # Đối chứng: cùng nhóm nhưng KHÁC scope vẫn phải được — NULLS NOT DISTINCT
    # chỉ gộp NULL lại, không biến index thành unique trên mỗi nhóm.
    other = _new_workspace(conn, actor, "grants-other")
    assert _attempt(conn, grant, scope=other, **args) is None


def test_exactly_one_principal_and_principal_type_matches_it(
    conn: sa.Connection, actor: uuid.UUID
) -> None:
    """Chứng minh 4/4 — `ck_role_assignment_one_principal` và
    `ck_role_assignment_principal_type`.

    Cả hai NULL thì hàng không thuộc về ai; cả hai có giá trị thì không rõ hàng
    nói về ai. Trường hợp cuối là cái mà `num_nonnulls = 1` KHÔNG bắt được, nên
    nó cần vế riêng: principal_type='group' trên một hàng có principal_user_id
    thoả num_nonnulls hoàn hảo (đúng một cột non-null) nhưng TỰ MÔ TẢ SAI về
    chính nó — Giai đoạn 1b trả principal_type ra giao diện danh sách quyền.
    """
    ws = _new_workspace(conn, actor, "principals")
    insert = (
        "INSERT INTO role_assignment (id, tenant_id, principal_type, principal_user_id,"
        " principal_group, scope_type, scope_id, role, created_by)"
        " VALUES (gen_random_uuid(), :tenant, :ptype, :puser, :pgroup, 'workspace', :scope,"
        " 'viewer', :actor)"
    )
    base: dict[str, object] = {"tenant": DEFAULT_TENANT_ID, "actor": actor, "scope": ws}

    both_null = _attempt(conn, insert, ptype="user", puser=None, pgroup=None, **base)
    assert both_null is not None, "hàng không thuộc về ai phải bị chặn"

    both_set = _attempt(conn, insert, ptype="user", puser=actor, pgroup="data-eng", **base)
    assert both_set is not None, "hàng vừa của người vừa của nhóm phải bị chặn"

    # Đúng num_nonnulls = 1, nhưng principal_type nói dối. Chỉ vế mới bắt được,
    # nên đây là phép kiểm CÔ LẬP cho nó.
    lying = _attempt(conn, insert, ptype="group", puser=actor, pgroup=None, **base)
    assert lying == "ck_role_assignment_principal_type", (
        f"principal_type='group' trên một hàng cấp quyền cho một NGƯỜI phải bị chặn, thấy: {lying}"
    )

    assert _attempt(conn, insert, ptype="user", puser=actor, pgroup=None, **base) is None
    other = _new_workspace(conn, actor, "principals-other")
    assert (
        _attempt(
            conn,
            insert,
            ptype="group",
            puser=None,
            pgroup="data-eng",
            tenant=DEFAULT_TENANT_ID,
            actor=actor,
            scope=other,
        )
        is None
    )


def test_stream_state_cursor_type_exists_and_stays_nullable(conn: sa.Connection) -> None:
    """Migration 0006 thêm `cursor_type`, và nó phải NULLABLE trên schema THẬT.

    Hai vế, cả hai đều là quyết định chứ không phải mặc định rơi ra:

    - CÓ MẶT: không có cột này thì phép so "watermark chỉ tiến" chỉ so được
      CHUỖI, và trên một cursor `bigint` chuỗi làm watermark kẹt vĩnh viễn ở lần
      đầu vượt mốc đổi số chữ số (xem `loom_core.cursor`).
    - NULLABLE: `ADD COLUMN ... NOT NULL` không kèm mặc định làm `alembic upgrade
      head` HỎNG trên mọi database đã có dù chỉ một hàng, còn một mặc định điền
      bừa (`'bigint'`) sẽ khiến lần báo tiến độ sau đọc một chuỗi ngày tháng như
      một số nguyên. Null = "hàng có từ trước 0006, không biết kiểu", và đường
      báo tiến độ ĐẶT LẠI watermark thay vì so sánh.

    Đọc `information_schema` chứ không đọc model: điều đang được khẳng định là
    migration đã chạy ra đúng schema đó, không phải là model khai đúng.
    """
    row = conn.execute(
        sa.text(
            "SELECT data_type, is_nullable, character_maximum_length"
            " FROM information_schema.columns"
            " WHERE table_name = 'stream_state' AND column_name = 'cursor_type'"
        )
    ).one_or_none()
    assert row is not None, "migration 0006 chưa thêm `stream_state.cursor_type`"
    assert (row[0], row[1]) == ("character varying", "YES")
    # Giá trị dài nhất trong `CURSOR_TYPE_ALLOWLIST` là 'timestamp without time
    # zone' (27 ký tự) — cột phải chứa nổi nó, nếu không một watermark hợp lệ bị
    # Postgres từ chối ở đúng kiểu ÍT được kiểm nhất.
    assert row[2] is not None and row[2] >= len("timestamp without time zone")


def test_stream_state_allows_only_one_watermark_per_stream(
    conn: sa.Connection, actor: uuid.UUID
) -> None:
    """Thiếu ràng buộc này thì hai watermark cùng tồn tại cho một stream, và lần
    nạp sau chọn bừa một cái — bỏ sót dữ liệu tuỳ theo cái nào được chọn.

    `uq_stream_state_lakehouse_connection_stream` CỐ Ý không có `cursor_column`
    trong khoá — hai hàng dưới đây khác nhau đúng ở cột đó, để phép kiểm này cô
    lập được chính điều đó: đổi cursor_column không được phép mở ra một hàng
    thứ hai cho cùng một stream.
    """
    ws = _new_workspace(conn, actor, "ingest")
    lakehouse = _new_item(conn, actor, ws, "lakehouse", "lh")
    connection = _new_item(conn, actor, ws, "connection", "conn")

    insert = (
        "INSERT INTO stream_state (id, lakehouse_id, connection_id, stream,"
        " cursor_column, cursor_value)"
        " VALUES (gen_random_uuid(), :lh, :cid, 'public.orders', :cursor_column, '1')"
    )
    base: dict[str, object] = {"lh": lakehouse, "cid": connection}

    assert _attempt(conn, insert, cursor_column="updated_at", **base) is None

    duplicate = _attempt(conn, insert, cursor_column="id", **base)
    assert duplicate == "uq_stream_state_lakehouse_connection_stream", (
        "hai watermark cho cùng (lakehouse, connection, stream) phải bị chặn dù "
        f"cursor_column khác nhau, thấy: {duplicate}"
    )

    # Đối chứng: cùng lakehouse/connection nhưng KHÁC stream vẫn phải được — ràng
    # buộc chỉ chặn trùng đúng bộ ba, không chặn mọi hàng của cùng một connection.
    other_stream = (
        "INSERT INTO stream_state (id, lakehouse_id, connection_id, stream,"
        " cursor_column, cursor_value)"
        " VALUES (gen_random_uuid(), :lh, :cid, 'public.customers', 'updated_at', '1')"
    )
    assert _attempt(conn, other_stream, **base) is None
