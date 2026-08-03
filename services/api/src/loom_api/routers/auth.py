import secrets

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from loom_api.oidc_client import TokenExchangeError
from loom_api.oidc_verifier import InvalidIdToken
from loom_api.sessions import CookieSigner, generate_pkce_pair
from loom_core.schemas import CurrentUser

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

    raw_tx = request.cookies.get(TX_COOKIE)
    if not raw_tx or not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "phiên đăng nhập không hợp lệ")

    try:
        transaction = _tx_signer(request).loads(raw_tx, max_age=TX_MAX_AGE)
    except Exception as exc:
        logger.warning("auth.tx_cookie_invalid", error=type(exc).__name__)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "phiên đăng nhập không hợp lệ") from exc

    if not secrets.compare_digest(str(transaction.get("state", "")), state):
        logger.warning("auth.state_mismatch")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "phiên đăng nhập không hợp lệ")

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
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "đăng nhập thất bại") from exc

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
async def me(request: Request) -> CurrentUser:
    settings = request.app.state.settings
    session_id = request.cookies.get(settings.session_cookie_name)
    if not session_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "chưa đăng nhập")

    try:
        claims = await request.app.state.user_store.load_session(session_id)
    except InvalidIdToken as exc:
        # Hàng phiên trong DB không dựng lại được thành danh tính hợp lệ (ví dụ
        # subject rỗng do dữ liệu hỏng). Với client thì phiên đơn giản là không
        # dùng được — 401, không phải 500.
        logger.warning("auth.session_unusable", reason=exc.reason)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "phiên đã hết hạn") from exc

    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "phiên đã hết hạn")

    return CurrentUser(subject=claims.subject, email=claims.email, display_name=claims.display_name)
