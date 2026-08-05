"""`NotVisible` và `Forbidden` nhìn từ phía DÂY, không phải từ phía đối tượng.

`test_permissions.py` khẳng định `exc.value.status_code == 404`. Đó là một câu
về ngoại lệ, không phải về thứ client nhận được — và hai thứ đó chỉ trùng nhau
KHI hai lớp này là lớp con của `HTTPException` và handler RFC 9457 nhận ra
chúng. Đổi lớp cha thành `Exception` giữ nguyên `status_code`, giữ nguyên mọi
test kia xanh, và biến mọi lần từ chối thành 500 text/plain.

Không cần database: chỉ cần hai ngoại lệ đi qua đúng chồng handler thật.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from loom_api.errors import PROBLEM_JSON, install_error_handlers
from loom_api.permissions import Forbidden, NotVisible


@pytest.fixture
async def client() -> AsyncClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/hidden")
    async def hidden() -> None:
        raise NotVisible

    @app.get("/denied")
    async def denied() -> None:
        raise Forbidden

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.parametrize(
    ("path", "status_code"),
    [("/hidden", 404), ("/denied", 403)],
)
async def test_the_two_denials_reach_the_client_as_problem_json(
    client: AsyncClient, path: str, status_code: int
) -> None:
    response = await client.get(path)

    assert response.status_code == status_code
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["status"] == status_code
    assert body["instance"] == path
    # `detail` phải có mặt: nó là thứ duy nhất giao diện hiển thị được, và
    # exclude_none của handler sẽ BỎ HẲN khoá này nếu detail là None.
    assert body["detail"]


async def test_the_two_denials_do_not_look_the_same_on_the_wire(client: AsyncClient) -> None:
    """Đối chứng cho test trên: một handler bắt cả hai rồi trả cùng một thứ sẽ
    làm nó xanh. Phân biệt 404 với 403 chỉ có nghĩa nếu client thấy được."""
    hidden = await client.get("/hidden")
    denied = await client.get("/denied")
    assert hidden.status_code != denied.status_code
    assert hidden.json()["title"] != denied.json()["title"]
