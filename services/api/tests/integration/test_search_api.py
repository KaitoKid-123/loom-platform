"""Tìm kiếm ⌘K — và trước hết là việc nó KHÔNG trả về thứ không được đọc.

Endpoint này không nhận `workspace_id` trong đường dẫn, nên không có gì nhắc người
viết phải lọc quyền. Mọi test dưới đây đều dựng một item trong workspace mà người
gọi KHÔNG có quyền, để một bản cài đặt quên lọc là đỏ ngay chứ không xanh vì thế
giới test tình cờ chỉ có dữ liệu hợp lệ.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from loom_api.models import DEFAULT_TENANT_ID, Item
from loom_core.roles import Role

pytestmark = pytest.mark.integration

SECRET = "acquisition-2026-finance"
MINE = "bao-cao-doanh-thu"


async def _insert_item(world, workspace_id: uuid.UUID, name: str, display_name: str) -> uuid.UUID:
    """Chèn thẳng một hàng `item` đã COMMIT.

    Không qua `POST /items`: cả điểm của test là dựng item trong workspace mà người
    gọi KHÔNG có quyền ghi, nên đường HTTP sẽ từ chối trước khi tạo được gì.
    """
    item_id = uuid.uuid4()
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Item(
                id=item_id,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type="sql_script",
                name=name,
                display_name=display_name,
                definition={"schema_version": 1, "sql": ""},
                definition_hash="x" * 64,
                created_by=world.user_id,
                updated_by=world.user_id,
            )
        )
        await session.commit()
    return item_id


@pytest.fixture
async def world(api_world):
    """Người gọi là viewer trên `ws_a` và KHÔNG có gì trên `ws_b`.

    `ws_b` chứa item bí mật. Cần nó trong MỌI test ở đây, kể cả những test nói về
    chuyện khác: một kết quả rỗng chỉ chứng minh được điều gì khi có một hàng mà
    một bản cài đặt hỏng SẼ trả về.
    """
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    await _insert_item(api_world, api_world.ws_b, SECRET, "Acquisition 2026")
    return api_world


@pytest.fixture
async def mine(world) -> uuid.UUID:
    """Một item người gọi ĐƯỢC đọc — đối chứng dương cho mọi phép kiểm rỗng."""
    return await _insert_item(world, world.ws_a, MINE, "Báo cáo doanh thu")


@pytest.fixture
async def literals(world) -> dict[str, uuid.UUID]:
    """Ba item thấy được, `display_name` mang ĐÚNG những ký tự đặc biệt của LIKE.

    `name` không thể chứa chúng — nó bị chặn bởi `^[a-z0-9][a-z0-9-]*$` ở biên — nên
    `display_name` là chỗ duy nhất chúng vào được, và người dùng gõ chính cái tên đó
    vào ⌘K.

    Đây là thứ biến phép kiểm escape từ "trả về rỗng" thành một khẳng định nhìn thấy
    được điều nó đặt tên: `q=%` phải trả về ĐÚNG item có dấu `%` thật, không phải cả
    ba. Một endpoint hỏng hoàn toàn cũng thoả "rỗng"; không gì thoả "đúng một".
    """
    return {
        "percent": await _insert_item(world, world.ws_a, "ke-hoach-mot", "Kế hoạch 100% xong"),
        "underscore": await _insert_item(world, world.ws_a, "ke-hoach-hai", "Kế hoạch_giai_hai"),
        # `\%` — hai ký tự cạnh nhau là ca DUY NHẤT phân biệt được thứ tự của ba phép
        # thay trong `_escape_like`. Nhân đôi dấu chéo ngược SAU cùng thì nó nhân đôi
        # cả dấu chéo do chính mình vừa thêm vào, và mẫu thành "hai dấu chéo liền
        # nhau" — không khớp gì cả.
        "both": await _insert_item(world, world.ws_a, "ke-hoach-ba", r"Kế hoạch \% đặc biệt"),
    }


async def test_search_never_returns_items_the_caller_cannot_read(world, mine):
    r = await world.client.get("/api/v1/search", params={"q": "acquisition"})
    assert r.status_code == 200, r.text
    names = [i["name"] for i in r.json()["items"]]
    assert names == [], f"rò rỉ item không được đọc: {names}"


async def test_search_finds_items_the_caller_can_read(world, mine):
    """Vế đối. Không có nó, một `return {"items": []}` vô điều kiện làm mọi phép
    kiểm rò rỉ ở trên xanh hết."""
    r = await world.client.get("/api/v1/search", params={"q": "doanh-thu"})
    assert {i["id"] for i in r.json()["items"]} == {str(mine)}


async def test_search_matches_display_name_too(world, mine):
    """Người dùng gõ tên họ THẤY trên giao diện, không phải slug kỹ thuật. `Báo cáo`
    chỉ có trong `display_name`."""
    r = await world.client.get("/api/v1/search", params={"q": "Báo cáo"})
    assert {i["id"] for i in r.json()["items"]} == {str(mine)}


async def test_search_is_case_insensitive(world, mine):
    """ASCII có chủ đích: `ilike` gấp chữ theo collation của database, và với dữ liệu
    ngoài ASCII thì kết quả phụ thuộc locale của container — một test xanh ở đây và
    đỏ trên Aiven không nói lên điều gì về code."""
    r = await world.client.get("/api/v1/search", params={"q": "BAO-CAO"})
    assert {i["id"] for i in r.json()["items"]} == {str(mine)}


async def test_empty_query_returns_empty_not_everything(world, mine):
    for q in ("", "   "):
        r = await world.client.get("/api/v1/search", params={"q": q})
        assert r.status_code == 200
        assert r.json()["items"] == [], f"q={q!r} trả về cả catalog"


async def test_query_is_optional(world, mine):
    """⌘K gọi endpoint này lúc mới mở, trước khi người dùng gõ gì. Thiếu default thì
    đó là một 422 ngay khi bảng lệnh xuất hiện."""
    r = await world.client.get("/api/v1/search")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        # `%` và `_` phải là KÝ TỰ THƯỜNG: mỗi query trả về đúng item mang ký tự đó,
        # không phải cả bốn item thấy được.
        # `both` cũng có dấu `%` (nó mang `\%`), nên nó phải nằm trong kết quả — bỏ
        # nó ra là biến khẳng định này thành một câu sai về dữ liệu.
        ("%", ("percent", "both")),
        ("_", ("underscore",)),
        (r"\%", ("both",)),
        # Không có item nào chứa hai ký tự này cạnh nhau — không escape thì `%_`
        # khớp mọi tên dài từ hai ký tự trở lên.
        ("%_", ()),
    ],
)
async def test_like_wildcards_are_treated_as_literal_characters(world, mine, literals, q, expected):
    r = await world.client.get("/api/v1/search", params={"q": q})
    assert r.status_code == 200, r.text
    assert {i["id"] for i in r.json()["items"]} == {str(literals[k]) for k in expected}, (
        f"q={q!r} không được xử lý như ký tự thường"
    )


async def test_a_bare_backslash_does_not_break_the_query(world, mine):
    """`\\` là ký tự escape của chính mẫu LIKE. Một mẫu kết thúc bằng escape rỗng là
    lỗi cú pháp phía Postgres, tức 500 vì một ký tự người dùng gõ được bằng một phím."""
    r = await world.client.get("/api/v1/search", params={"q": "\\"})
    assert r.status_code == 200
    assert r.json()["items"] == []


async def test_a_stranger_searching_gets_nothing(api_world):
    """Không một vai trò nào ở đâu cả. Kết quả rỗng, không phải 403 — bảng lệnh
    không có gì để nói về những workspace mà với người này thì không tồn tại."""
    await _insert_item(api_world, api_world.ws_a, "bi-mat-hoan-toan", "Bí mật")
    r = await api_world.client.get("/api/v1/search", params={"q": "bi-mat"})
    assert r.status_code == 200
    assert r.json()["items"] == []


async def test_a_soft_deleted_item_leaves_the_search_results(api_world):
    """`visible_items_select` lọc `state`, và search phải thừa hưởng điều đó. Một
    item đã xoá còn hiện trong ⌘K là người dùng bấm vào rồi ăn 404.

    Tự dựng thế giới thay vì dùng fixture `world`: cần vai trò admin để xoá được,
    và gán vai trò thứ hai cho cùng người + cùng phạm vi sẽ đụng
    `uq_role_assignment_principal_scope`.
    """
    w = api_world
    await w.grant(("workspace", w.ws_a), Role.admin)
    await _insert_item(w, w.ws_b, SECRET, "Acquisition 2026")
    item_id = await _insert_item(w, w.ws_a, MINE, "Báo cáo doanh thu")

    before = await w.client.get("/api/v1/search", params={"q": "doanh-thu"})
    assert {i["id"] for i in before.json()["items"]} == {str(item_id)}

    assert (await w.client.delete(f"/api/v1/items/{item_id}")).status_code == 204

    after = await w.client.get("/api/v1/search", params={"q": "doanh-thu"})
    assert after.json()["items"] == []


async def test_search_results_carry_only_the_fields_the_palette_needs(world, mine):
    """Không có `definition`. Với item `connection` thì definition mang `secret_ref`,
    và ⌘K là bề mặt hiện cho nhiều người nhất trong cả ứng dụng. Khẳng định trên ĐÚNG
    bộ khoá, không chỉ `"definition" not in`: phép kiểm phủ định đó cũng đúng với một
    dict rỗng."""
    r = await world.client.get("/api/v1/search", params={"q": "doanh-thu"})
    row = r.json()["items"][0]
    assert set(row) == {"id", "workspace_id", "type", "name", "display_name", "folder_path"}


async def test_an_unauthenticated_caller_gets_401(world, mine):
    r = await world.client.get(
        "/api/v1/search", params={"q": "doanh-thu"}, cookies={"loom_session": "sai"}
    )
    assert r.status_code == 401
