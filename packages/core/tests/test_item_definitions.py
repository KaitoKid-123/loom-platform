import pytest
from pydantic import ValidationError

from loom_core.item_definitions import (
    DEFAULT_DEFINITION,
    DEFINITION_BY_TYPE,
    K8S_SECRET_REF_RE,
    SECRET_REF_RE,
    ConnectionDefinition,
    ItemType,
    canonical_hash,
    parse_definition,
)

# Một payload HỢP LỆ cho từng loại. Cần cho các test dưới đây vì chúng phải
# tách được "đỏ vì trường lạ" khỏi "đỏ vì thiếu trường bắt buộc".
VALID_RAW: dict[ItemType, dict] = {
    ItemType.lakehouse: {"schema_version": 1},
    ItemType.connection: {
        "schema_version": 1,
        "kind": "postgres",
        "host": "db.local",
        "port": 5432,
        "secret_ref": "vault://loom/db#password",
    },
    ItemType.pipeline: {"schema_version": 1, "steps": []},
    ItemType.sql_script: {"schema_version": 1, "sql": ""},
}


def test_valid_raw_covers_every_item_type():
    """Thêm một `ItemType` mà quên payload ở đây thì các test dưới lặng lẽ bỏ
    qua loại mới, và một `model_config` đặt lại trong lớp mới đó không ai thấy."""
    assert set(VALID_RAW) == set(ItemType)


def test_typo_in_field_name_is_rejected():
    """extra='forbid' chứ không phải 'ignore': gõ sai `hots` thay vì `host` phải
    đỏ ngay lúc ghi, không im lặng nằm trong database tới Giai đoạn 3.

    Trường bị gõ sai ở đây là `database`, một trường TUỲ CHỌN, và mọi trường bắt
    buộc đều có mặt. Đó là điều kiện để phép kiểm này nhìn thấy được thứ nó nói:
    gõ sai `host` thành `hots` cũng làm mất một trường BẮT BUỘC, nên bản ghi đỏ
    kể cả khi `extra` là 'ignore' — test kiểu đó xanh với cả hai cấu hình và
    không chứng minh gì về `extra`. Đã kiểm bằng mutation: đổi 'forbid' thành
    'ignore' KHÔNG làm phiên bản `hots` đỏ.
    """
    ConnectionDefinition(**VALID_RAW[ItemType.connection])  # tiền đề: payload hợp lệ
    with pytest.raises(ValidationError):
        ConnectionDefinition(**VALID_RAW[ItemType.connection], databse="loom")


def test_typo_that_also_drops_a_required_field_is_rejected():
    """Tình huống thật của người dùng — `hots` thay vì `host`. Giữ lại như một
    test hồi quy, nhưng nó KHÔNG phải bằng chứng cho `extra='forbid'`: xem
    docstring ở trên."""
    with pytest.raises(ValidationError):
        ConnectionDefinition(
            schema_version=1,
            kind="postgres",
            hots="db.local",
            port=5432,
            secret_ref="vault://loom/db#password",
        )


@pytest.mark.parametrize("item_type", list(ItemType))
def test_every_definition_type_forbids_unknown_fields(item_type):
    """`extra='forbid'` nằm trên `_Base`, nhưng chỉ kiểm nó trên
    `ConnectionDefinition` thì một lớp con đặt lại `model_config` sẽ lọt.
    Trường lạ trong `pipeline` hay `sql_script` cũng đi vào `definition`, vào
    `item_version` và vào Git y như trường lạ trong `connection`.

    Khẳng định payload gốc HỢP LỆ trước: không có dòng đó thì `ValidationError`
    có thể đến từ một trường bắt buộc bị thiếu, và phép kiểm mù với `extra`."""
    parse_definition(item_type, VALID_RAW[item_type])
    with pytest.raises(ValidationError):
        parse_definition(item_type, VALID_RAW[item_type] | {"truong_khong_ton_tai": 1})


def test_schema_version_must_be_one():
    """`Literal[1]`, không phải `int`. Một bản ghi mang schema_version=2 mà
    Giai đoạn 1 vẫn nhận là một bản ghi không đọc nổi theo hình dạng nào."""
    with pytest.raises(ValidationError):
        parse_definition(ItemType.sql_script, {"schema_version": 2, "sql": "SELECT 1"})


@pytest.mark.parametrize(
    "ref",
    [
        "vault://loom/prod/db#password",
        "k8s://loom/loom-db-app#password",
    ],
)
def test_valid_secret_ref_accepted(ref):
    ConnectionDefinition(
        schema_version=1, kind="postgres", host="db.local", port=5432, secret_ref=ref
    )


@pytest.mark.parametrize(
    "ref",
    [
        "password123",  # mật khẩu thật, không phải tham chiếu
        "vault://loom/db",  # thiếu #key
        "http://loom/db#password",  # scheme không được phép
        "vault://#password",  # thiếu path
        "",
        # Một DSN dán nguyên từ console của nhà cung cấp — đây là hình dạng THẬT
        # của sự cố mà regex này tồn tại để chặn, và nó mang cả username lẫn
        # mật khẩu. (Chuỗi giả: tiền tố mật khẩu thật của Aiven và hostname thật
        # đều bị scripts/git-hooks/pre-commit chặn, kể cả trong test.)
        "postgresql://loom_app:n0t-a-real-secret@db.internal.example:26257/defaultdb?sslmode=require",
        # Cùng DSN đó nhưng có dấu `#` ở cuối: đủ để lọt qua một regex chỉ đòi
        # "có # và có gì đó sau nó".
        "postgresql://loom_app:n0t-a-real-secret@db.internal.example:26257/db#password",
        # `$` của Python khớp cả trước một `\n` cuối chuỗi, nên `^...$` một mình
        # nhận chuỗi nhiều dòng. Xem SECRET_REF_RE — nó dùng `\Z`.
        "vault://loom/db#password\n",
        "vault://loom/db#password\npassword123",
        "vault://loom/db#",  # có `#` nhưng không có key
    ],
)
def test_invalid_secret_ref_rejected(ref):
    """Chặn ở đây là lớp phòng vệ chống việc người dùng dán MẬT KHẨU THẬT vào ô
    secret_ref. Nếu nhận bừa thì credential nằm trong definition, mà definition
    đi vào item_version, audit summary và Git ở Giai đoạn 5."""
    with pytest.raises(ValidationError):
        ConnectionDefinition(
            schema_version=1, kind="postgres", host="db.local", port=5432, secret_ref=ref
        )


@pytest.mark.parametrize(
    "ref",
    [
        "k8s://loom/loom-db-app#password",
        "k8s://a/b#c",
        "k8s://loom-prod-01/pg.credentials-1#POSTGRES_PASSWORD",
        "k8s://x9-y/z.9-a#K_e-y.1",
    ],
)
def test_every_k8s_ref_the_ingest_path_accepts_is_one_items_would_store(ref):
    """`K8S_SECRET_REF_RE` phải là tập CON của `SECRET_REF_RE`.

    Đường nạp (`loom_api.ingest_service.resolve_secret_ref`) dựa vào quan hệ
    bao hàm này: nó chỉ bao giờ nhìn thấy những `secret_ref` mà
    `ConnectionDefinition` đã nhận và đã lưu. Nếu bản `k8s://` có nhóm bắt hẹp
    hơn ở một chỗ nào đó, hậu quả không phải là một lỗi cú pháp mà là một
    connection ĐANG SỐNG bỗng không nạp được, với một 400 nói "not usable" về
    đúng dữ liệu mà API đã chấp thuận. Hai regex dựng từ cùng các lớp ký tự
    (xem `item_definitions.py`) nên hôm nay quan hệ đó là hiển nhiên — phép
    kiểm này tồn tại để nó vẫn hiển nhiên sau lần ai đó nới một trong hai bên.
    """
    assert K8S_SECRET_REF_RE.match(ref), ref
    assert SECRET_REF_RE.match(ref), ref


@pytest.mark.parametrize(
    "ref",
    [
        "vault://loom/prod/db#password",  # nhánh vault: hợp lệ, nhưng không phải k8s
        "k8s://loom/db#password\n",
        "k8s://LOOM/db#password",  # namespace hoa: cả hai đều từ chối
        "k8s://loom/db",  # thiếu #key
    ],
)
def test_the_k8s_only_pattern_never_accepts_what_the_general_one_rejects(ref):
    """Chiều còn lại của quan hệ bao hàm: không có chuỗi nào lọt qua bản hẹp mà
    bản chung từ chối. `vault://...` có mặt ở đây vì nó là trường hợp DUY NHẤT
    hợp lệ với bản chung nhưng phải trượt bản hẹp — đường nạp dựa vào đúng điều
    đó để đưa ra thông báo "cụm này không tới được Vault"."""
    assert not K8S_SECRET_REF_RE.match(ref) or SECRET_REF_RE.match(ref)


def test_parse_definition_dispatches_on_type():
    d = parse_definition(ItemType.sql_script, {"schema_version": 1, "sql": "SELECT 1"})
    assert d.sql == "SELECT 1"
    with pytest.raises(ValidationError):
        parse_definition(ItemType.sql_script, {"schema_version": 1, "khong_co": 1})


def test_parse_definition_covers_every_item_type():
    """Thiếu một loại trong `DEFINITION_BY_TYPE` là `KeyError` lúc chạy, ở đúng
    đường ghi item — không phải chỗ muốn phát hiện thiếu sót."""
    assert set(DEFINITION_BY_TYPE) == set(ItemType)


def test_every_default_definition_parses():
    """Mặc định mà không qua nổi chính schema của nó thì mọi item tạo mới của
    loại đó đỏ. `connection` CỐ Ý không có mặc định."""
    assert ItemType.connection not in DEFAULT_DEFINITION
    for item_type, raw in DEFAULT_DEFINITION.items():
        parse_definition(item_type, raw)


def test_canonical_hash_is_stable_across_key_order():
    """Hash phải bằng nhau khi cùng nội dung khác thứ tự khoá, nếu không mỗi lần
    lưu lại sinh một version mới dù không đổi gì.

    Khoá lồng nhau là chủ ý: sắp xếp mỗi tầng ngoài cùng thôi thì test này vẫn
    xanh trong khi `{"c":2,"d":3}` và `{"d":3,"c":2}` cho ra hai hash."""
    a = canonical_hash({"a": 1, "b": {"c": 2, "d": 3}})
    b = canonical_hash({"b": {"d": 3, "c": 2}, "a": 1})
    assert a == b
    assert len(a) == 64


def test_canonical_hash_changes_when_content_changes():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_canonical_hash_distinguishes_list_order():
    """Thứ tự KHOÁ không có nghĩa, thứ tự PHẦN TỬ thì có: `nodes` của một
    pipeline đổi thứ tự là một thay đổi thật. Một cách chuẩn hoá quá tay
    (sắp xếp cả list) sẽ làm hai pipeline khác nhau ra cùng một hash."""
    assert canonical_hash({"nodes": [1, 2]}) != canonical_hash({"nodes": [2, 1]})


# --- PipelineStep & ScheduleDefinition (Task 2) --------------------------------


def test_a_schedule_that_is_enabled_must_name_who_it_runs_as() -> None:
    """`run_as_user_id` BẮT BUỘC khi `enabled` — kiểm ở BIÊN, không phải một `if`
    trong scheduler."""
    with pytest.raises(ValidationError):
        parse_definition(
            ItemType.pipeline,
            {
                "schema_version": 1,
                "steps": [],
                "schedule": {"enabled": True, "cron": "0 2 * * *", "timezone": "UTC"},
            },
        )


def test_a_bad_cron_is_refused_when_the_item_is_saved() -> None:
    with pytest.raises(ValidationError):
        parse_definition(
            ItemType.pipeline,
            {
                "schema_version": 1,
                "steps": [],
                "schedule": {
                    "enabled": False,
                    "cron": "khong phai cron",
                    "timezone": "UTC",
                },
            },
        )


def test_a_bad_timezone_is_refused_when_the_item_is_saved() -> None:
    with pytest.raises(ValidationError):
        parse_definition(
            ItemType.pipeline,
            {
                "schema_version": 1,
                "steps": [],
                "schedule": {
                    "enabled": False,
                    "cron": "0 2 * * *",
                    "timezone": "Mars/Olympus",
                },
            },
        )


def test_an_ingest_step_needs_its_ingest_fields() -> None:
    with pytest.raises(ValidationError):
        parse_definition(
            ItemType.pipeline,
            {"schema_version": 1, "steps": [{"type": "ingest"}]},
        )


def test_a_pipeline_with_no_schedule_is_valid() -> None:
    """Một pipeline chạy tay là hợp lệ — `schedule` là tuỳ chọn."""
    parsed = parse_definition(ItemType.pipeline, {"schema_version": 1, "steps": []})
    assert parsed.schedule is None
