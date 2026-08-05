import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import jwt
import pytest
import structlog.testing
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from loom_api.oidc_verifier import InvalidIdToken, OIDCVerifier

ISSUER = "http://loom.localhost/dex"
CLIENT_ID = "loom"


def make_keypair(kid: str = "test-key") -> tuple[RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private_key, {"keys": [jwk]}


def sign(private_key: RSAPrivateKey, kid: str = "test-key", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "CgRsb25n",
        "email": "long@loom.local",
        "name": "Long",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def verifier_for(jwks: dict[str, Any]) -> OIDCVerifier:
    async def fetch() -> dict[str, Any]:
        return jwks

    return OIDCVerifier(issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch)


async def test_valid_token_returns_claims() -> None:
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key))
    assert claims.subject == "CgRsb25n"
    assert claims.email == "long@loom.local"
    assert claims.display_name == "Long"


async def test_expired_token_is_rejected() -> None:
    key, jwks = make_keypair()
    now = int(time.time())
    token = sign(key, iat=now - 600, exp=now - 300)
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(token)


async def test_wrong_audience_is_rejected() -> None:
    key, jwks = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(key, aud="someone-else"))


async def test_wrong_issuer_is_rejected() -> None:
    key, jwks = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(key, iss="http://evil.example/dex"))


async def test_unknown_kid_is_rejected() -> None:
    key, jwks = make_keypair(kid="real-key")
    token = sign(key, kid="forged-key")
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(token)


async def test_token_signed_by_other_key_is_rejected() -> None:
    _, jwks = make_keypair()
    attacker_key, _ = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(attacker_key))


async def test_jwks_is_fetched_once_then_cached() -> None:
    key, jwks = make_keypair()
    calls = 0

    async def fetch() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return jwks

    verifier = OIDCVerifier(issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch)
    await verifier.verify(sign(key))
    await verifier.verify(sign(key))
    assert calls == 1


async def test_cache_is_refreshed_once_on_unknown_kid() -> None:
    """Dex xoay khoá — lần đầu gặp kid lạ phải nạp lại JWKS, không được fail ngay."""
    old_key, old_jwks = make_keypair(kid="old")
    new_key, new_jwks = make_keypair(kid="new")
    responses = [old_jwks, new_jwks]

    async def fetch() -> dict[str, Any]:
        return responses.pop(0) if len(responses) > 1 else responses[0]

    # min_refresh_interval_seconds=0.0: hai lần verify() sát nhau trong test này
    # cố tình đi qua đường xoay khoá ngay lập tức, nên phải tắt sàn thời gian
    # chống dội (mặc định 10s) mà nó sẽ chặn lần nạp lại thứ hai.
    verifier = OIDCVerifier(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        fetch_jwks=fetch,
        min_refresh_interval_seconds=0.0,
    )
    await verifier.verify(sign(old_key, kid="old"))
    claims = await verifier.verify(sign(new_key, kid="new"))
    assert claims.subject == "CgRsb25n"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def test_forged_hs256_token_is_rejected() -> None:
    """Nhầm lẫn thuật toán: ký HS256 bằng chính public key RSA làm khoá HMAC.

    Phải tự dựng JWS bằng tay — PyJWT từ chối encode kiểu này, còn kẻ tấn công
    thật thì tính HMAC trực tiếp nên không bị chặn.
    """
    from cryptography.hazmat.primitives import serialization

    key, jwks = make_keypair()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "test-key"}).encode())
    payload = _b64(
        json.dumps(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "attacker",
                "iat": now,
                "exp": now + 300,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())

    with pytest.raises(InvalidIdToken) as caught:
        await verifier_for(jwks).verify(f"{header}.{payload}.{signature}")
    # Phải bị chặn bởi allow-list (PyJWT ném InvalidAlgorithmError), KHÔNG phải
    # rơi vào catch-all. Nếu ai đó gỡ pin algorithms=["RS256"], PyJWT sẽ thử
    # HMAC, vấp TypeError, và reason thành "unexpected_error" — test đỏ.
    assert caught.value.reason == "verification_failed"


async def test_alg_none_token_is_rejected() -> None:
    _, jwks = make_keypair()
    now = int(time.time())
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": "test-key"}).encode())
    payload = _b64(
        json.dumps(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": now, "exp": now + 300}
        ).encode()
    )
    with pytest.raises(InvalidIdToken) as caught:
        await verifier_for(jwks).verify(f"{header}.{payload}.")
    assert caught.value.reason == "verification_failed"


async def test_one_malformed_jwks_entry_does_not_break_good_keys() -> None:
    key, jwks = make_keypair()
    jwks["keys"].insert(0, {"kid": "broken", "kty": "RSA"})  # thiếu n/e
    claims = await verifier_for(jwks).verify(sign(key))
    assert claims.subject == "CgRsb25n"


async def test_empty_subject_is_rejected() -> None:
    key, jwks = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(key, sub=""))


async def test_groups_claim_is_read_from_the_token() -> None:
    """Đây là điểm DUY NHẤT nhóm đi từ IdP vào hệ thống. Test round-trip ở
    integration dựng IdTokenClaims bằng tay nên KHÔNG chạm dòng này — nếu
    `verify()` bỏ qua claim `groups`, chỉ test này thấy."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups=["data-eng", "admins"]))
    assert claims.groups == ("admins", "data-eng")


async def test_groups_claim_is_deduplicated_and_sorted() -> None:
    """Đưa vào giảm dần và có trùng; đi ra tăng dần, không trùng. Tám tên để một
    mutation bỏ `sorted()` không sống sót nhờ may mắn về thứ tự băm của set."""
    key, jwks = make_keypair()
    names = ["zulu", "yankee", "xray", "whiskey", "victor", "uniform", "tango", "sierra"]
    claims = await verifier_for(jwks).verify(sign(key, groups=[*names, "zulu"]))
    assert claims.groups == tuple(sorted(names))


async def test_blank_group_names_in_the_token_are_dropped() -> None:
    """Một claim `groups` có phần tử rỗng KHÔNG được làm đăng nhập thất bại —
    nhưng cũng không được đi vào session: `principal_group = ''` trong
    role_assignment sẽ khớp với nó."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups=["admins", "", "  "]))
    assert claims.groups == ("admins",)


async def test_missing_groups_claim_means_no_groups() -> None:
    """Dex chưa phát `groups` cho tới Task 25. Trước đó token không có claim đó,
    và điều đó phải là "không có nhóm", không phải một lỗi."""
    key, jwks = make_keypair()
    assert (await verifier_for(jwks).verify(sign(key))).groups == ()


# --------------------------------------------------------------------------
# Claim `groups` là dữ liệu do IdP điều khiển và KHÔNG có kiểu. Cả khối dưới
# đây tồn tại vì hai lỗi thật, mỗi lỗi tìm được bằng một token ký thật:
#
#   `{str(g) for g in raw}` trên một chuỗi trần `"admins"` cho ra sáu nhóm
#   một-ký-tự trông hợp lệ — chốt của Principal không cứu được vì verify() đã
#   làm phẳng chuỗi trước khi Principal nhìn thấy nó;
#
#   `groups: 123` ném TypeError SAU khối try/except của jwt.decode, nên nó
#   thoát khỏi verify() và thành 500 text/plain ở /auth/callback.
#
# Mỗi hình dạng dưới đây phải có một kết cục ĐƯỢC ĐỊNH NGHĨA. Không ca nào
# được phép ném thứ gì ngoài InvalidIdToken.
# --------------------------------------------------------------------------


async def test_bare_string_groups_claim_is_not_split_into_characters() -> None:
    """Lỗ hổng: `"admins"` iterate ra `('a','d','i','m','n','s')`. Sáu nhóm
    một-ký-tự đó đi vào user_session.groups và thành principal của RBAC ở
    Task 13 — một grant cho nhóm tên `a` sẽ âm thầm áp dụng."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups="admins"))
    assert claims.groups == ()
    assert "a" not in claims.groups


async def test_space_delimited_groups_claim_yields_no_groups() -> None:
    """Nhóm phân cách bởi dấu cách là hình dạng claim thường gặp ở IdP. Nó
    KHÔNG được tách thành nhóm — kể cả tách theo dấu cách, vì đoán định dạng của
    một claim sai kiểu là tự bịa ra quyền."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups="admins data-eng"))
    assert claims.groups == ()


async def test_non_iterable_groups_claim_is_invalid_id_token_not_type_error() -> None:
    """`groups: 123` là ca đã thành HTTP 500 text/plain ở /auth/callback: một
    TypeError, không phải InvalidIdToken, nên `except (TokenExchangeError,
    InvalidIdToken)` của callback không bắt được."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups=123))
    assert claims.groups == ()


async def test_dict_groups_claim_yields_no_groups() -> None:
    """Một object JSON cũng iterate được — ra KHOÁ của nó. `{"admins": true}`
    sẽ thành nhóm `admins` mà không ai cấp."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups={"admins": True}))
    assert claims.groups == ()


async def test_null_groups_claim_means_no_groups_and_is_not_reported_as_malformed() -> None:
    """`groups: null` là "không có nhóm", KHÔNG phải claim hỏng. Trước đây điều
    này phụ thuộc vào `or []`; giờ nó là một nhánh tường minh, và test khẳng
    định cả sự IM LẶNG — bỏ nhánh `raw is None` thì `null` rơi vào phép kiểm
    not-a-list và bắn cảnh báo giả cho một token hoàn toàn bình thường."""
    key, jwks = make_keypair()
    with structlog.testing.capture_logs() as logs:
        claims = await verifier_for(jwks).verify(sign(key, groups=None))
    assert claims.groups == ()
    assert [entry["event"] for entry in logs] == []


async def test_malformed_groups_claim_is_logged_for_the_operator() -> None:
    """Đối trọng của test trên: claim sai kiểu bị bỏ qua ÂM THẦM thì RBAC hỏng
    theo kiểu không chẩn đoán được ("sao chẳng ai có nhóm nào?"). Kết cục là
    fail-closed, nhưng phải có dấu vết."""
    key, jwks = make_keypair()
    with structlog.testing.capture_logs() as logs:
        await verifier_for(jwks).verify(sign(key, groups="admins"))
    assert [entry["event"] for entry in logs] == ["oidc.groups_claim_not_a_list"]
    assert logs[0]["claim_type"] == "str"


async def test_non_string_entries_in_the_groups_list_are_dropped_not_stringified() -> None:
    """`str(5)` là `'5'` và `str(None)` là `'None'` — hai tên nhóm hợp lệ mà
    không ai định nghĩa. Bỏ hẳn, giống hệt quy tắc đã có cho phần tử rỗng, và
    KHÔNG làm đăng nhập thất bại."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups=["admins", 5, None, True]))
    assert claims.groups == ("admins",)


async def test_groups_list_containing_an_empty_string_keeps_the_rest() -> None:
    """Trùng ý với test_blank_group_names_in_the_token_are_dropped nhưng phát
    biểu ở dạng ca biên mà review đòi: danh sách CÓ phần tử rỗng vẫn đăng nhập
    được, và tên rỗng không đi vào session."""
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key, groups=["", "admins"]))
    assert claims.groups == ("admins",)


async def test_empty_subject_keeps_its_own_reason_through_the_widened_try() -> None:
    """Việc đọc claim đã chuyển VÀO trong try của jwt.decode. Không có nhánh
    `except InvalidIdToken: raise`, `IdTokenClaims.__post_init__` ném
    "empty_subject" sẽ bị catch-all ghi đè thành "unexpected_error" và chẩn đoán
    trong log biến mất."""
    key, jwks = make_keypair()
    with pytest.raises(InvalidIdToken) as caught:
        await verifier_for(jwks).verify(sign(key, sub=""))
    assert caught.value.reason == "empty_subject"


async def test_concurrent_unknown_kid_triggers_single_fetch() -> None:
    key, jwks = make_keypair()
    calls = 0

    async def fetch() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)  # nhường điều khiển, mở cửa cho thundering herd
        return jwks

    verifier = OIDCVerifier(issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch)
    token = sign(key)
    await asyncio.gather(*(verifier.verify(token) for _ in range(50)))
    assert calls == 1


async def test_revoked_key_is_rejected_once_cache_expires() -> None:
    """Thu hồi khẩn cấp: nhà cung cấp xoá hẳn khoá, không thay bằng kid mới."""
    key, jwks = make_keypair()
    live = {"keys": list(jwks["keys"])}

    async def fetch() -> dict[str, Any]:
        return live

    verifier = OIDCVerifier(
        issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch, cache_ttl_seconds=0.0
    )
    assert (await verifier.verify(sign(key))).subject == "CgRsb25n"

    live["keys"] = []  # khoá bị thu hồi
    with pytest.raises(InvalidIdToken):
        await verifier.verify(sign(key))
