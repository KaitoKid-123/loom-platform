"""Endpoint item qua đúng đường HTTP, tập trung vào ETag.

ETag chỉ có giá trị nếu client NHẬN được nó và server TÔN TRỌNG nó. Test ở tầng
store kiểm được nửa sau; nửa trước chỉ thấy khi đi qua HTTP.
"""

import uuid

import pytest

from loom_core.roles import Role

pytestmark = pytest.mark.integration

_SQL = {"schema_version": 1, "sql": "SELECT 1"}


async def _make_item(world, name: str = "mot-item") -> tuple[uuid.UUID, str]:
    r = await world.client.post(
        f"/api/v1/workspaces/{world.ws_a}/items",
        json={
            "type": "sql_script",
            "name": name,
            "display_name": name,
            "definition": _SQL,
        },
    )
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"]), r.headers["etag"]


@pytest.fixture
async def contributor(api_world):
    await api_world.grant(("workspace", api_world.ws_a), Role.contributor)
    return api_world


async def test_create_returns_an_etag_so_no_extra_get_is_needed(contributor):
    """Không có ETag trên phản hồi tạo, client phải GET lại trước khi sửa được
    thứ mình vừa tạo."""
    _, etag = await _make_item(contributor)
    assert etag == 'W/"1"'


async def test_get_returns_an_etag_matching_the_version(contributor):
    item_id, _ = await _make_item(contributor)
    r = await contributor.client.get(f"/api/v1/items/{item_id}")
    assert r.status_code == 200
    assert r.headers["etag"] == 'W/"1"'
    assert r.json()["version"] == 1


async def test_patch_without_if_match_is_428(contributor):
    """428 Precondition Required, KHÔNG phải 400: client biết chính xác phải thêm
    header nào, và một client tử tế tự thử lại đúng cách."""
    item_id, _ = await _make_item(contributor)
    r = await contributor.client.patch(f"/api/v1/items/{item_id}", json={"display_name": "X"})
    assert r.status_code == 428
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_patch_with_a_stale_if_match_is_412(contributor):
    item_id, etag = await _make_item(contributor)
    ok = await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "A"},
        headers={"If-Match": etag},
    )
    assert ok.status_code == 200
    assert ok.headers["etag"] == 'W/"2"'

    stale = await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "B"},
        headers={"If-Match": etag},
    )
    assert stale.status_code == 412
    # Thông báo của server nói bản hiện tại là mấy — đó là thông tin duy nhất giúp
    # người dùng hiểu chuyện gì vừa xảy ra.
    assert "2" in stale.json()["detail"]


@pytest.mark.parametrize("form", ['W/"1"', '"1"', "1", " 1 "])
async def test_if_match_accepts_both_weak_and_bare_forms(contributor, form):
    """Client HTTP và proxy viết lại ETag. Biến một chi tiết định dạng thành 412 là
    cách chắc chắn để người dùng tin công việc của họ vừa bị mất."""
    item_id, _ = await _make_item(contributor, name=f"dinh-dang-{abs(hash(form)) % 999}")
    r = await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "Đổi"},
        headers={"If-Match": form},
    )
    assert r.status_code == 200, f"{form!r} bị từ chối: {r.text}"


async def test_a_malformed_if_match_is_400_not_412(contributor):
    """400 vì client gửi rác, không phải 412 — 412 nghĩa là 'bản của bạn cũ', một
    câu sai và gây hoang mang ở đây."""
    item_id, _ = await _make_item(contributor)
    r = await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "X"},
        headers={"If-Match": "khong-phai-so"},
    )
    assert r.status_code == 400


async def test_a_noop_patch_keeps_the_same_etag(contributor):
    """`PATCH` không đổi gì không bump version, nên ETag phải giữ nguyên — nếu nó
    tăng thì client tin rằng có thay đổi và lịch sử đầy bản ghi trùng."""
    item_id, etag = await _make_item(contributor)
    r = await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "mot-item", "definition": _SQL},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    assert r.headers["etag"] == etag


async def test_invalid_folder_path_is_rejected_at_the_boundary(contributor):
    """`/a/b` không có gạch chéo cuối. Nhận cả hai dạng thì cây trên UI hiện hai
    nhánh cho cùng một folder và không ai hiểu vì sao."""
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "sql_script",
            "name": "folder-sai",
            "display_name": "Folder sai",
            "folder_path": "/a/b",
            "definition": _SQL,
        },
    )
    assert r.status_code == 422
    assert any(e["loc"][-1] == "folder_path" for e in r.json()["errors"])


@pytest.mark.parametrize("where", ["body", "query"])
async def test_an_unknown_item_type_is_422_not_500(contributor, where):
    """`ItemType("notebook")` là `ValueError`, và không ai bắt nó thì client nhận 500
    với một thân phản hồi cố ý không nói gì. Người gọi không biết `notebook` không
    tồn tại, không biết bốn loại hợp lệ là gì, và không có gì để sửa.

    Cả HAI chỗ: body của lệnh tạo và query của lệnh liệt kê. Mỗi chỗ là một
    constructor riêng nhận thẳng dữ liệu client gửi, nên chúng hỏng riêng được.
    """
    if where == "body":
        r = await contributor.client.post(
            f"/api/v1/workspaces/{contributor.ws_a}/items",
            json={
                "type": "notebook",
                "name": "khong-co-loai-nay",
                "display_name": "Không có loại này",
                "definition": _SQL,
            },
        )
    else:
        r = await contributor.client.get(
            f"/api/v1/workspaces/{contributor.ws_a}/items", params={"type": "notebook"}
        )

    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    # Phản hồi phải LIỆT KÊ các loại hợp lệ, không chỉ nói là sai.
    assert "sql_script" in r.text


async def test_a_pasted_password_in_secret_ref_is_rejected(contributor):
    """Chặn ở đây là lớp phòng vệ chống việc dán MẬT KHẨU THẬT vào ô secret_ref.
    Lọt một lần là credential đi vào definition, item_version, audit và Git."""
    r = await contributor.client.post(
        f"/api/v1/workspaces/{contributor.ws_a}/items",
        json={
            "type": "connection",
            "name": "ket-noi",
            "display_name": "Kết nối",
            "definition": {
                "schema_version": 1,
                "kind": "postgres",
                "host": "db.local",
                "port": 5432,
                "secret_ref": "mat-khau-that-cua-toi",
            },
        },
    )
    assert r.status_code == 422


async def test_restore_returns_a_new_etag_not_the_old_one(contributor):
    """`restore` sinh version MỚI mang nội dung cũ. Trả lại ETag cũ sẽ khiến client
    gửi If-Match sai ở lần sửa kế tiếp."""
    item_id, etag = await _make_item(contributor)
    await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "Đã đổi"},
        headers={"If-Match": etag},
    )
    r = await contributor.client.post(f"/api/v1/items/{item_id}/versions/1/restore")
    assert r.status_code == 200
    assert r.headers["etag"] == 'W/"3"'
    assert r.json()["display_name"] == "mot-item"


async def test_versions_list_never_exposes_the_definition(contributor):
    """Với item `connection` thì definition mang `secret_ref`, và danh sách version
    thường hiện cho nhiều người hơn bản thân item."""
    item_id, etag = await _make_item(contributor)
    await contributor.client.patch(
        f"/api/v1/items/{item_id}",
        json={"display_name": "V2"},
        headers={"If-Match": etag},
    )
    r = await contributor.client.get(f"/api/v1/items/{item_id}/versions")
    assert r.status_code == 200
    rows = r.json()["items"]
    assert len(rows) == 2
    assert all("definition" not in row for row in rows)
    # Và khẳng định có đúng những khoá mong đợi — "không có definition" cũng đúng
    # với một danh sách rỗng hoặc một dict trống.
    assert set(rows[0]) == {
        "version",
        "display_name",
        "folder_path",
        "description",
        "change_note",
        "created_at",
        "created_by",
    }


async def test_paging_items_never_repeats_a_row_when_updated_at_ties(contributor):
    """Mọi item tạo trong cùng transaction chia nhau một `updated_at`. Khoá sắp xếp
    thiếu `id` thì lật trang lặp hoặc nhảy bản ghi."""
    for i in range(7):
        await _make_item(contributor, name=f"trang-{i}")

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        url = f"/api/v1/workspaces/{contributor.ws_a}/items?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        body = (await contributor.client.get(url)).json()
        seen.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "không lật hết được trong 10 vòng"
    assert len(seen) == 7, f"mong 7 item, nhận {len(seen)}"
    assert len(set(seen)) == 7, "có bản ghi lặp giữa các trang"


async def test_a_viewer_cannot_create_an_item(api_world):
    await api_world.grant(("workspace", api_world.ws_a), Role.viewer)
    r = await api_world.client.post(
        f"/api/v1/workspaces/{api_world.ws_a}/items",
        json={
            "type": "sql_script",
            "name": "khong-duoc",
            "display_name": "Không được",
            "definition": _SQL,
        },
    )
    assert r.status_code == 403


async def test_a_stranger_listing_items_gets_404(api_world):
    """404 chứ không danh sách rỗng: với người không có quyền nào thì workspace
    không tồn tại, và một trang rỗng lại khẳng định là nó có."""
    r = await api_world.client.get(f"/api/v1/workspaces/{api_world.ws_a}/items")
    assert r.status_code == 404
