from typing import Annotated

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import AfterValidator, BaseModel

from loom_api.errors import install_error_handlers


def _must_be_even(v: int) -> int:
    if v % 2:
        raise ValueError("phải là số chẵn")
    return v


class _EvenBody(BaseModel):
    n: Annotated[int, AfterValidator(_must_be_even)]


@pytest.fixture
async def app_client():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(403, "không đủ quyền")

    @app.get("/typed")
    async def typed(n: int) -> dict[str, int]:
        return {"n": n}

    @app.post("/even")
    async def even(body: _EvenBody) -> dict[str, int]:
        return {"n": body.n}

    @app.get("/needs-auth")
    async def needs_auth() -> None:
        raise HTTPException(
            401, "chưa đăng nhập", headers={"WWW-Authenticate": 'Bearer realm="loom"'}
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_http_exception_becomes_problem_json(app_client):
    r = await app_client.get("/boom")
    assert r.status_code == 403
    # Content-Type PHẢI là problem+json, không phải application/json. Frontend
    # phân biệt lỗi có cấu trúc với lỗi lạ bằng đúng header này.
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 403
    assert body["detail"] == "không đủ quyền"
    assert body["title"] == "Forbidden"


async def test_validation_error_becomes_problem_json(app_client):
    r = await app_client.get("/typed?n=khong-phai-so")
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 422
    # `errors` giữ chi tiết từng trường để frontend gắn được lỗi vào đúng input.
    assert isinstance(body["errors"], list)
    assert body["errors"][0]["loc"] == ["query", "n"]


async def test_inapplicable_members_are_omitted_not_null(app_client):
    """RFC 9457: thành viên không áp dụng thì BỎ HẲN khỏi body, không để null.
    Frontend kiểm `"errors" in body` để biết có chi tiết từng trường hay không —
    gửi null thì phép kiểm đó luôn đúng và frontend đi sai nhánh. Bỏ
    `exclude_none=True` khỏi handler thì test này đỏ."""
    r = await app_client.get("/boom")
    body = r.json()
    assert "errors" not in body
    assert "instance" in body  # instance CÓ áp dụng, phải còn


async def test_validation_error_with_unserialisable_ctx_stays_422(app_client):
    """Validator tự viết raise ValueError -> Pydantic để nguyên object ValueError
    trong `ctx`, mà nó không JSON-serialise được. Không lọc `ctx` ra thì
    json.dumps nổ TypeError NGAY TRONG handler của 422, biến một 422 hợp lệ
    thành 500. Bỏ bộ lọc khoá an toàn khỏi handler thì test này đỏ."""
    r = await app_client.post("/even", json={"n": 3})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    err = r.json()["errors"][0]
    assert err["loc"] == ["body", "n"]
    assert err["type"] == "value_error"
    # Chỉ ba khoá an toàn. `ctx` mang ValueError, `input` vọng lại dữ liệu người
    # dùng vừa gửi — cả hai đều không được ra khỏi đây.
    assert set(err) == {"loc", "msg", "type"}


# --------------------------------------------------------------------------
# Header của HTTPException. Với vài mã trạng thái, header là một phần BẮT BUỘC
# của giao thức, không phải trang trí — và handler ở trên đã im lặng đánh rơi
# chúng kể từ khi nó giành quyền xử lý HTTPException.
# --------------------------------------------------------------------------


async def test_http_exception_headers_are_preserved(app_client):
    """`WWW-Authenticate` trên 401 là BẮT BUỘC theo RFC 9110 §15.5.2. FastAPI
    gửi nó trước Task 1; handler problem+json bỏ `exc.headers` nên nó biến mất,
    và một 401 không có `WWW-Authenticate` là một phản hồi sai giao thức."""
    r = await app_client.get("/needs-auth")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="loom"'
    # Header tự thêm KHÔNG được cướp mất content-type: Starlette chỉ tự điền
    # content-type khi headers không có sẵn khoá đó, nên phải kiểm cả hai.
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 401


async def test_wrong_method_on_a_real_route_still_carries_allow(client):
    """Ca sống, trên app thật: `Allow` trên 405 là BẮT BUỘC theo RFC 9110
    §15.5.6 — nó là thứ nói cho client biết phải thử phương thức nào. Starlette
    ném HTTPException(405, headers={"Allow": ...}), nên header này chỉ tồn tại
    nếu handler problem+json chuyển tiếp `exc.headers`.

    Dùng fixture `client` (create_app thật) chứ không phải app đồ chơi ở trên:
    405 do ROUTER sinh ra, không phải do endpoint nào, nên chỉ chuỗi thật mới
    kiểm được nhánh này."""
    response = await client.head("/api/v1/me")
    assert response.status_code == 405
    assert response.headers["allow"]
    assert "GET" in response.headers["allow"]
