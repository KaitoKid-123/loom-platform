from typing import Annotated

import httpx
import pytest
import structlog.contextvars
import structlog.testing
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import AfterValidator, BaseModel

from loom_api.errors import UNEXPECTED_ERROR_DETAIL, install_error_handlers
from loom_api.main import create_app

# Trông giống hệt thứ một thông điệp ngoại lệ thật hay mang theo: đường dẫn nội
# bộ, tên host, một giá trị bí mật. Mỗi mẩu được kiểm riêng ở dưới, nên không
# một cách rò rỉ từng phần nào lọt qua.
SECRET_MESSAGE = "/etc/loom/aiven.env: password=hunter2 host=pg-loom.aivencloud.com"
REQUEST_ID = "rid-9f2c-from-the-edge"


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

    @app.get("/dict-detail")
    async def dict_detail() -> None:
        # `detail` của Starlette khai kiểu Any và nhận dict thật. Handler phải
        # sống sót được với nó.
        raise HTTPException(400, {"code": "workspace_locked", "workspace": "w-1"})

    @app.get("/unhandled")
    async def unhandled() -> None:
        raise ValueError(SECRET_MESSAGE)

    # raise_app_exceptions=False: ServerErrorMiddleware của Starlette ném LẠI
    # ngoại lệ sau khi handler đã dựng xong phản hồi (để server thật log được
    # nó), nên nếu không tắt cờ này thì test client nổ trước khi nhìn thấy
    # phản hồi mà nó cần kiểm.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
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


# --------------------------------------------------------------------------
# Ngoại lệ chưa xử lý. Commit của Task 1 nói "return RFC 9457 problem details
# for every error" và spec mục 6 đòi đúng thế; cả hai đều sai cho tới khi có
# handler dưới đây — không đăng ký handler nào cho `Exception` nghĩa là mọi lỗi
# lạ rơi xuống ServerErrorMiddleware của Starlette và ra ngoài dưới dạng
# `Internal Server Error` text/plain.
# --------------------------------------------------------------------------


async def test_unhandled_exception_becomes_problem_json(app_client):
    r = await app_client.get("/unhandled")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 500
    assert body["title"] == "Internal Server Error"
    # `instance` là thứ nối phản hồi này với dòng log tương ứng.
    assert body["instance"] == "/unhandled"


async def test_unhandled_exception_leaks_nothing_from_the_message(app_client):
    """Thông điệp ngoại lệ mang đường dẫn nội bộ, host và giá trị đang xử lý.
    Không mẩu nào được rời khỏi log. Kiểm trên TOÀN BỘ thân phản hồi, không chỉ
    trường `detail`: một handler "hữu ích" nhét str(exc) vào `errors` hay vào
    `title` cũng phải đỏ."""
    r = await app_client.get("/unhandled")
    raw = r.text
    for leak in ("hunter2", "aiven.env", "aivencloud.com", "/etc/loom", "ValueError"):
        assert leak not in raw, leak
    assert r.json()["detail"] == UNEXPECTED_ERROR_DETAIL


async def test_unhandled_exception_logs_the_traceback_with_the_request_id():
    """Thân phản hồi im lặng chỉ chấp nhận được nếu log thì không. `request_id`
    là sợi dây duy nhất nối "người dùng báo trang lỗi lúc 14:03" với traceback
    thật, nên nó phải nằm trên chính dòng log đó.

    Chạy trên app THẬT: request_id do RequestContextMiddleware bind vào
    contextvars, nên app đồ chơi trong file này không kiểm được nhánh này.
    """

    class ExplodingStore:
        async def load_session(self, session_id: str) -> None:
            raise RuntimeError(SECRET_MESSAGE)

    app = create_app(
        user_store=ExplodingStore(),
        oidc_http=httpx.AsyncClient(),
        verify_id_token=None,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # create_app() gọi configure_logging(), mà configure_logging() thay
            # cả danh sách processor — nên nó phải chạy XONG trước khi vào
            # capture_logs, không thì bản ghi bị thổi bay.
            with structlog.testing.capture_logs(
                processors=[structlog.contextvars.merge_contextvars]
            ) as logs:
                r = await ac.get(
                    "/api/v1/me",
                    headers={
                        # Cookie phải có, nếu không get_principal trả 401 trước
                        # khi chạm tới store và ngoại lệ không bao giờ xảy ra.
                        "Cookie": "loom_session=s-1",
                        "X-Request-ID": REQUEST_ID,
                    },
                )

    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert SECRET_MESSAGE not in r.text

    entries = [e for e in logs if e["event"] == "http.unhandled_exception"]
    assert len(entries) == 1, [e["event"] for e in logs]
    entry = entries[0]
    assert entry["request_id"] == REQUEST_ID
    assert entry["exc_type"] == "RuntimeError"
    # Ngoại lệ THẬT, không phải True: `exc_info=True` chỉ đúng khi
    # sys.exc_info() còn giá trị, và đó là chi tiết nội bộ của Starlette.
    assert isinstance(entry["exc_info"], RuntimeError)
    assert str(entry["exc_info"]) == SECRET_MESSAGE


async def test_dict_detail_does_not_become_a_500(app_client):
    """`ProblemDetail.detail` là `str | None`. Starlette khai `HTTPException.detail`
    là Any và code thật đặt dict vào đó. Bỏ phép kiểm `isinstance(exc.detail, str)`
    thì Pydantic ném ValidationError NGAY TRONG handler lỗi, Starlette bắt lấy và
    trả 500 text/plain — một 400 sạch sẽ biến thành sự cố."""
    r = await app_client.get("/dict-detail")
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 400
    assert body["title"] == "Bad Request"
    # Bỏ HẲN chứ không stringify: `"{'code': 'workspace_locked'}"` là repr của
    # Python rò ra ngoài dây, và `detail` theo RFC 9457 là câu giải thích cho
    # người đọc, không phải một cấu trúc.
    assert "detail" not in body


async def test_problem_type_is_about_blank(app_client):
    """RFC 9457: `type` vắng mặt được hiểu là "about:blank". Ghi rõ ra là hợp
    lệ và là điều ProblemDetail chọn — nhưng chuỗi RỖNG thì không: một URI rỗng
    tham chiếu chính tài liệu hiện tại, nên client nào phân nhánh theo `type`
    sẽ gộp mọi lỗi vào một loại. Không test nào chạm tới giá trị này trước đây."""
    for path, status in (("/boom", 403), ("/dict-detail", 400)):
        body = (await app_client.get(path)).json()
        assert body["type"] == "about:blank", (path, status)
    validation = (await app_client.get("/typed?n=x")).json()
    assert validation["type"] == "about:blank"
