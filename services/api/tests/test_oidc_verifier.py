import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import jwt
import pytest
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
