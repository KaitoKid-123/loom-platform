"""Nói chuyện với nhà cung cấp OIDC qua HTTP — mang client_secret, có I/O."""

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from loom_api.oidc_verifier import InvalidIdToken
from loom_core.config import Settings

logger = structlog.get_logger(__name__)


class TokenExchangeError(Exception):
    """Nhà cung cấp OIDC từ chối đổi code lấy token."""


@dataclass(frozen=True)
class OIDCEndpoints:
    authorization_endpoint: str  # địa chỉ công khai — trình duyệt dùng
    token_endpoint: str  # địa chỉ nội bộ — pod dùng
    jwks_uri: str  # địa chỉ nội bộ — pod dùng


@dataclass(frozen=True)
class TokenSet:
    id_token: str
    access_token: str | None
    refresh_token: str | None


# `groups` là scope BẮT BUỘC phải yêu cầu, không phải một thứ tuỳ chọn nice-to-have.
# OIDC không phát claim tuỳ chọn mà client không xin: thiếu nó ở đây thì Dex im lặng
# trả một id_token không có `groups`, `_normalise_groups` nhận `None` và trả tuple
# rỗng, và toàn bộ RBAC theo nhóm chết mà không một dòng log nào nói gì. Đó chính là
# trạng thái của hệ thống cho tới task này.
SCOPES = "openid email profile offline_access groups"


class OIDCClient:
    """Nói chuyện với nhà cung cấp OIDC.

    Trình duyệt và pod nhìn Dex ở hai địa chỉ khác nhau. Endpoint dành cho
    trình duyệt giữ nguyên địa chỉ công khai; endpoint gọi từ server được
    viết lại sang địa chỉ nội bộ trong cụm.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._endpoints: OIDCEndpoints | None = None
        self._endpoints_lock = asyncio.Lock()

    def _to_internal(self, url: str) -> str:
        internal = self._settings.oidc_internal_base
        public = self._settings.public_base_url
        if not internal:
            return url
        # Kiểm biên chứ không startswith trần: "http://loom.localhost" là tiền
        # tố chuỗi của "http://loom.localhost.evil.example", nên startswith sẽ
        # viết lại tên miền của kẻ tấn công thành địa chỉ nội bộ — và
        # exchange_code sẽ POST client_secret sang đó.
        if url == public or url.startswith(public + "/"):
            return internal + url[len(public) :]
        logger.info("oidc.endpoint_not_rewritten", url=url, public_base=public)
        return url

    async def endpoints(self) -> OIDCEndpoints:
        if self._endpoints is not None:
            return self._endpoints
        async with self._endpoints_lock:
            # Kiểm lại sau khi giành khoá — coroutine khác có thể vừa nạp xong.
            if self._endpoints is None:
                discovery_url = self._to_internal(
                    f"{self._settings.oidc_issuer}/.well-known/openid-configuration"
                )
                # 5s chứ không phải 10s: đây là đường đi của một redirect người dùng
                # đang chờ. Dex sống thì mọi lời gọi này ở mức mili-giây; Dex chết
                # thì hỏng nhanh tốt hơn treo lâu. Giai đoạn 0 chưa có circuit breaker.
                response = await self._http.get(discovery_url, timeout=5.0)
                response.raise_for_status()
                document = response.json()

                self._endpoints = OIDCEndpoints(
                    authorization_endpoint=document["authorization_endpoint"],
                    token_endpoint=self._to_internal(document["token_endpoint"]),
                    jwks_uri=self._to_internal(document["jwks_uri"]),
                )
        return self._endpoints

    async def authorization_url(self, state: str, code_challenge: str) -> str:
        endpoints = await self.endpoints()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.oidc_client_id,
                "redirect_uri": self._settings.oidc_redirect_url,
                "scope": SCOPES,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoints.authorization_endpoint}?{query}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        endpoints = await self.endpoints()
        response = await self._http.post(
            endpoints.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.oidc_redirect_url,
                "client_id": self._settings.oidc_client_id,
                "client_secret": self._settings.oidc_client_secret,
                "code_verifier": code_verifier,
            },
            timeout=5.0,
        )
        if response.status_code != 200:
            raise TokenExchangeError(f"nhà cung cấp trả về {response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            raise TokenExchangeError("phản hồi không phải JSON hợp lệ") from exc

        if not isinstance(payload, dict) or "id_token" not in payload:
            raise TokenExchangeError("phản hồi thiếu id_token")
        return TokenSet(
            id_token=payload["id_token"],
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
        )

    async def fetch_jwks(self) -> dict[str, Any]:
        endpoints = await self.endpoints()
        try:
            response = await self._http.get(endpoints.jwks_uri, timeout=5.0)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
        except Exception as exc:
            # Task 6 đã bịt lỗi *entry* JWKS hỏng, nhưng lỗi *vận chuyển* (Dex
            # không tới được, timeout, JSON rác) vẫn thoát nguyên si khỏi
            # verify() và thành 500. Gói lại thành InvalidIdToken để handler
            # của Task 8 bắt được và trả 401.
            logger.warning("oidc.jwks_fetch_failed", error=type(exc).__name__)
            raise InvalidIdToken("jwks_unavailable") from exc
        return result
