"""Chuyển mọi lỗi thành RFC 9457 Problem Details.

Giai đoạn 0 định nghĩa ProblemDetail nhưng không nối vào đâu, nên API trả
`{"detail": "..."}` mặc định của FastAPI. Frontend không phân biệt được lỗi có
cấu trúc với lỗi lạ, và mất luôn chi tiết từng trường của lỗi validation."""

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


def _response(problem: ProblemDetail) -> JSONResponse:
    # exclude_none: RFC 9457 nói thành viên không áp dụng thì BỎ HẲN, chứ không
    # phải để null. Client kiểm `"errors" in body` sẽ sai nếu ta gửi null.
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_JSON,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail = exc.detail if isinstance(exc.detail, str) else None
    return _response(
        ProblemDetail(
            title=_title(exc.status_code),
            status=exc.status_code,
            detail=detail,
            instance=str(request.url.path),
        )
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
