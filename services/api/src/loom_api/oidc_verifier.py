"""Giao thức OIDC — tách khỏi tầng HTTP để test được mà không cần dựng server."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt
import structlog
from jwt import PyJWK

JWKSFetcher = Callable[[], Awaitable[dict[str, Any]]]

logger = structlog.get_logger(__name__)


class InvalidIdToken(Exception):
    """ID token không hợp lệ.

    Thông điệp con người là một hằng số cố ý: nó có thể bị stringify ở bất kỳ
    đâu mà không tiết lộ giai đoạn xác minh nào đã thất bại. Lý do máy đọc nằm
    ở `.reason`, chỉ dùng cho log.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("id token verification failed")


@dataclass(frozen=True)
class IdTokenClaims:
    subject: str
    email: str
    display_name: str
    # Chuẩn hoá ở verify() (dedupe + sort), không phải ở đây, vì __post_init__ của
    # một dataclass frozen không gán lại được trường. Người gọi dựng trực tiếp thì
    # tự chịu thứ tự của mình — Principal chuẩn hoá lại ở đầu bên kia dù sao.
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Bất biến nằm trên chính kiểu dữ liệu, không nằm ở verify(): Task 8
        # dựng lại IdTokenClaims thẳng từ hàng trong DB (load_session), tức là
        # đi vòng qua verify() hoàn toàn.
        if not self.subject.strip():
            raise InvalidIdToken("empty_subject")


def _normalise_groups(raw: object) -> tuple[str, ...]:
    """Chuẩn hoá claim `groups`. Đây là điểm DUY NHẤT nhóm đi từ IdP vào hệ thống.

    Claim này KHÔNG được ép kiểu. Một chuỗi trần `"admins"` — hình dạng thường
    gặp khi IdP phát nhóm phân cách bởi dấu cách — cũng iterate được, và
    `{str(g) for g in raw}` biến nó thành sáu nhóm một-ký-tự trông hoàn toàn hợp
    lệ. Chúng đi vào `user_session.groups` rồi thành principal của RBAC, nên một
    grant cho `role_assignment.principal_group = 'a'` sẽ âm thầm khớp.
    `Principal._normalise` có chốt cho đúng ca này, nhưng chốt đó vô dụng nếu
    chỗ này đã làm phẳng chuỗi trước khi Principal kịp nhìn thấy nó.

    Claim sai kiểu được coi là VẮNG MẶT (không nhóm) chứ không làm hỏng đăng
    nhập. Lý do: `groups` là claim tuỳ chọn — Dex chưa phát nó tới tận Task 25,
    và "không nhóm" vì thế đã là một trạng thái được định nghĩa sẵn, fail-closed
    với RBAC. Biến một claim tuỳ chọn sai kiểu thành `InvalidIdToken` sẽ biến
    một cấu hình sai ở phía IdP thành sự cố KHÔNG AI ĐĂNG NHẬP ĐƯỢC, tức là đổi
    một lỗi phân quyền lấy một lỗi xác thực nặng hơn. Dòng log dưới đây là thứ
    để người vận hành chẩn đoán, và nó chỉ phát ra khi claim thật sự sai kiểu —
    `null` (hay vắng mặt) là hợp lệ và phải im lặng.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        # `list` chứ không phải `Iterable`: claim tới từ JSON, nên mảng JSON là
        # `list` và không gì khác. str, int, dict đều rơi vào đây.
        logger.warning("oidc.groups_claim_not_a_list", claim_type=type(raw).__name__)
        return ()

    # Phần tử không phải chuỗi (hay chuỗi rỗng) bị BỎ, không làm đăng nhập thất
    # bại: một claim lệch chuẩn ở phía IdP không được chặn người dùng đăng nhập.
    # Nhưng nó cũng không được ép kiểu và đi vào session — `str(None)` là
    # `'None'` và `str(5)` là `'5'`, hai tên nhóm hợp lệ mà không ai định nghĩa,
    # và `principal_group = ''` trong role_assignment khớp với một tên rỗng.
    invalid = [g for g in raw if not isinstance(g, str) or not g.strip()]
    if invalid:
        logger.warning("oidc.groups_claim_has_invalid_entries", count=len(invalid))
    return tuple(sorted({g.strip() for g in raw if isinstance(g, str) and g.strip()}))


class OIDCVerifier:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        fetch_jwks: JWKSFetcher,
        cache_ttl_seconds: float = 300.0,
        min_refresh_interval_seconds: float = 10.0,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._fetch_jwks = fetch_jwks
        self._cache_ttl_seconds = cache_ttl_seconds
        self._min_refresh_interval_seconds = min_refresh_interval_seconds
        self._keys: dict[str, PyJWK] | None = None
        self._loaded_at = 0.0
        self._reload_lock = asyncio.Lock()

    async def _load_keys(self) -> dict[str, PyJWK]:
        document = await self._fetch_jwks()
        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except Exception as exc:
                # Một khoá hỏng không được phép hạ gục các khoá còn tốt.
                logger.warning("oidc.jwks_entry_invalid", kid=kid, error=type(exc).__name__)
        return keys

    def _needs_reload(self, kid: str) -> bool:
        if self._keys is None:
            return True
        age = time.monotonic() - self._loaded_at
        if age >= self._cache_ttl_seconds:
            return True
        # kid lạ được phép nạp lại sớm, nhưng có sàn thời gian: nếu không, kẻ
        # gửi kid rác ép được đúng một lần fetch cho mỗi request.
        return kid not in self._keys and age >= self._min_refresh_interval_seconds

    async def _key_for(self, kid: str) -> PyJWK:
        if self._needs_reload(kid):
            async with self._reload_lock:
                # Kiểm lại sau khi giành được khoá: một coroutine khác có thể
                # vừa nạp xong trong lúc ta chờ.
                if self._needs_reload(kid):
                    self._keys = await self._load_keys()
                    self._loaded_at = time.monotonic()

        key = (self._keys or {}).get(kid)
        if key is None:
            raise InvalidIdToken("unknown_kid")
        return key

    async def verify(self, id_token: str) -> IdTokenClaims:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise InvalidIdToken("malformed_header") from exc

        kid = header.get("kid")
        if not kid:
            raise InvalidIdToken("missing_kid")

        key = await self._key_for(kid)
        # Việc ĐỌC CLAIM nằm trong cùng try với jwt.decode, không phải sau nó.
        # Claim là dữ liệu do IdP điều khiển và chưa được kiểm kiểu: `groups: 123`
        # từng ném TypeError ở đây, tức là SAU cái catch-all bên dưới, nên nó
        # thoát khỏi verify(), thoát tiếp qua `except (TokenExchangeError,
        # InvalidIdToken)` trong callback và thành 500 text/plain — đúng thứ mà
        # catch-all này tồn tại để chặn.
        try:
            payload = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            subject = str(payload["sub"])
            email = str(payload.get("email") or "")
            claims = IdTokenClaims(
                subject=subject,
                email=email,
                display_name=str(payload.get("name") or email or subject),
                groups=_normalise_groups(payload.get("groups")),
            )
        except jwt.PyJWTError as exc:
            raise InvalidIdToken("verification_failed") from exc
        except InvalidIdToken:
            # Đã là InvalidIdToken với lý do RIÊNG của nó (IdTokenClaims ném
            # "empty_subject"). Nuốt nó vào catch-all bên dưới sẽ ghi đè lý do
            # thành "unexpected_error" và xoá mất chẩn đoán trong log.
            raise
        except Exception as exc:
            # PyJWT ném được cả thứ không phải PyJWTError (ví dụ TypeError từ
            # force_bytes khi khoá sai kiểu). Không để nó thoát ra thành 500 —
            # nhưng phải mang mã lý do RIÊNG, vì "PyJWT từ chối token" và
            # "có gì đó hỏng bất ngờ" là hai chuyện khác nhau, và test canh
            # allow-list dựa vào đúng sự khác biệt này.
            logger.warning("oidc.verify_unexpected_error", error=type(exc).__name__)
            raise InvalidIdToken("unexpected_error") from exc

        return claims
