import secrets

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse

from loom_api.deps import PrincipalDep
from loom_api.oidc_client import TokenExchangeError
from loom_api.oidc_verifier import InvalidIdToken
from loom_api.sessions import CookieSigner, generate_pkce_pair
from loom_core.schemas import CurrentUser, Principal

TX_COOKIE = "loom_oidc_tx"
TX_MAX_AGE = 600

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["auth"])


def _tx_signer(request: Request) -> CookieSigner:
    return CookieSigner(request.app.state.settings.session_secret, salt="loom-oidc-tx")


@router.get("/auth/login")
async def login(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    url = await request.app.state.oidc_client.authorization_url(
        state=state, code_challenge=challenge
    )
    response = RedirectResponse(url)
    response.set_cookie(
        TX_COOKIE,
        _tx_signer(request).dumps({"state": state, "verifier": verifier}),
        max_age=TX_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/api/v1/auth",
    )
    return response


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    settings = request.app.state.settings

    # callback chỉ bao giờ được tới qua redirect toàn trang từ Dex — không có
    # caller lập trình nào. Vì vậy MỌI nhánh lỗi mà người dùng có thể chạm tới
    # phải trả về một trang họ điều hướng được, không phải JSON thô không lối
    # thoát. Chi tiết (reason) vẫn chỉ vào log — KHÔNG đưa vào query string vì
    # cùng lý do oracle như str(exc): "?error=login_failed" cố ý chung một câu
    # cho mọi giai đoạn thất bại.
    raw_tx = request.cookies.get(TX_COOKIE)
    if not raw_tx or not code or not state:
        return RedirectResponse("/?error=login_failed")

    try:
        transaction = _tx_signer(request).loads(raw_tx, max_age=TX_MAX_AGE)
    except Exception as exc:
        logger.warning("auth.tx_cookie_invalid", error=type(exc).__name__)
        return RedirectResponse("/?error=login_failed")

    if not secrets.compare_digest(str(transaction.get("state", "")), state):
        logger.warning("auth.state_mismatch")
        return RedirectResponse("/?error=login_failed")

    try:
        tokens = await request.app.state.oidc_client.exchange_code(
            code=code, code_verifier=transaction["verifier"]
        )
        claims = await request.app.state.verify_id_token(tokens.id_token)
    except (TokenExchangeError, InvalidIdToken) as exc:
        # KHÔNG BAO GIỜ đưa str(exc) ra client. Thông điệp của InvalidIdToken
        # phân biệt được theo giai đoạn thất bại ("kid không có trong JWKS" khác
        # "chữ ký sai"), tức là một oracle cho kẻ dò. Chi tiết chỉ vào log.
        logger.warning(
            "auth.exchange_failed",
            error=type(exc).__name__,
            reason=getattr(exc, "reason", None),
        )
        return RedirectResponse("/?error=login_failed")

    session_id = await request.app.state.user_store.upsert_user_and_create_session(
        claims, tokens.refresh_token
    )
    logger.info("auth.login_succeeded", subject=claims.subject)

    response = RedirectResponse("/")
    response.delete_cookie(TX_COOKIE, path="/api/v1/auth")
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> Response:
    settings = request.app.state.settings
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        await request.app.state.user_store.delete_session(session_id)
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=CurrentUser)
async def me(principal: Principal = PrincipalDep) -> CurrentUser:
    # Không còn đọc cookie ở đây: xem loom_api.deps. Handler nào cũng tự đọc
    # cookie thì chỉ cần một handler quên là có một endpoint công khai.
    #
    # KHÔNG chạm database, và giữ đúng như thế. Đã thử đặt `tenant_role` vào đây và đó là
    # sai chỗ: `/me` gọi mỗi lần tải trang, nên nó thành một round trip nữa cho một câu
    # hỏi mà endpoint danh sách workspace trả lời được miễn phí — nó đã chạm database rồi
    # và giao diện đã gọi nó ở đúng trang cần biết. Xem `WorkspaceListOut.tenant_role`.
    return CurrentUser(
        # Từ `Principal`, tức từ BỘ NHỚ — `PrincipalDep` đã dựng nó từ phiên trước khi
        # handler chạy. Nên trường này KHÔNG phá bất biến "không chạm database" ở trên;
        # nó chép một giá trị đã có sẵn.
        user_id=principal.user_id,
        subject=principal.subject,
        email=principal.email,
        display_name=principal.display_name,
        groups=principal.groups,
    )
