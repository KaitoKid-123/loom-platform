"""Chuyển mọi lỗi thành RFC 9457 Problem Details.

Giai đoạn 0 định nghĩa ProblemDetail nhưng không nối vào đâu, nên API trả
`{"detail": "..."}` mặc định của FastAPI. Frontend không phân biệt được lỗi có
cấu trúc với lỗi lạ, và mất luôn chi tiết từng trường của lỗi validation."""

from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from loom_core.schemas import ProblemDetail

PROBLEM_JSON = "application/problem+json"


def _title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _response(problem: ProblemDetail, headers: Mapping[str, str] | None = None) -> JSONResponse:
    # exclude_none: RFC 9457 nói thành viên không áp dụng thì BỎ HẲN, chứ không
    # phải để null. Client kiểm `"errors" in body` sẽ sai nếu ta gửi null.
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_JSON,
        headers=dict(headers) if headers else None,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    # detail của Starlette được khai kiểu Any và code thật SỰ đặt dict vào đó
    # (chi tiết lỗi theo trường). ProblemDetail.detail là `str | None`, nên đưa
    # thẳng một dict vào sẽ ném ValidationError NGAY TRONG handler lỗi và
    # Starlette trả 500 text/plain — biến một 400 sạch sẽ thành một sự cố.
    detail = exc.detail if isinstance(exc.detail, str) else None
    return _response(
        ProblemDetail(
            title=_title(exc.status_code),
            status=exc.status_code,
            detail=detail,
            instance=str(request.url.path),
        ),
        # Với nhiều mã trạng thái, header KHÔNG phải trang trí mà là bắt buộc
        # theo giao thức: `Allow` trên 405 (RFC 9110 §15.5.6), `WWW-Authenticate`
        # trên 401 (§15.5.2), `Retry-After` trên 429/503. FastAPI gửi chúng cho
        # tới khi handler này giành quyền xử lý HTTPException và bỏ rơi
        # `exc.headers`. getattr chứ không `exc.headers`: StarletteHTTPException
        # có thuộc tính này, nhưng handler được đăng ký cho một kiểu cơ sở và
        # phải sống sót với mọi lớp con mà người khác ném vào.
        getattr(exc, "headers", None),
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # errors() trả về ValidationError của Pydantic, trong đó `ctx` có thể chứa
    # object không JSON-serialise được (ví dụ ValueError). Lọc xuống các khoá an
    # toàn thay vì để JSONResponse nổ 500 khi đang xử lý một lỗi 422.
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return _response(
        ProblemDetail(
            title=_title(422),
            status=422,
            detail="dữ liệu gửi lên không hợp lệ",
            instance=str(request.url.path),
            errors=errors,
        )
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
